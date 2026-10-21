from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def packaging():
    path = (Path(__file__).parents[2] / "plans/261015-ocr-nvidia-recovery"
            / "package-rtx50-test.py")
    spec = importlib.util.spec_from_file_location("rtx50_test_packaging", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("relative,allowed", [
    ("Scripts/python.exe", True), (".venv/Scripts/pythonw.exe", True),
    (".venv/Scripts/python311.dll", True), (".venv/Scripts/vcruntime140.dll", True),
    (".venv/Scripts/paddleocr.exe", False), ("Scripts/activate", False),
    (".venv/Scripts/Activate.ps1", False),
    (".venv/Lib/site-packages/paddle/base.py", True),
    (".venv/Lib/site-packages/paddle/base.pyd", True),
    (".venv/Lib/site-packages/certifi/cacert.pem", True),
    ("Lib/json/__init__.py", True), ("Lib/__pycache__/os.pyc", False),
    ("Lib/test/test_abc.py", False),
    (".venv/Lib/site-packages/Crypto/SelfTest/key.pem", False),
    (".venv/Lib/site-packages/paddle.dist-info/direct_url.json", False),
    ("Lib/ensurepip/_bundled/pip.whl", False),
])
def test_runtime_filter_keeps_libraries_but_not_machine_bound_or_test_files(
    packaging, relative, allowed,
):
    assert packaging.runtime_file_allowed(Path(relative)) is allowed


@pytest.mark.parametrize("relative", ["../private.txt", "/private.txt", "C:/private.txt",
                                      "sub/../../private.txt", "sub\\private.txt", "."])
def test_manifest_paths_cannot_escape_package_root(packaging, tmp_path, relative):
    with pytest.raises(ValueError, match="relative package path"):
        packaging.checked_path(tmp_path, relative)


def test_package_audit_rejects_private_material_but_keeps_public_ca(packaging, tmp_path):
    public_ca = tmp_path / "cacert.pem"
    public_ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----")
    assert packaging.file_digest(public_ca, audit=True)
    private_key = tmp_path / "private.txt"
    private_key.write_bytes(b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 40)
    with pytest.raises(ValueError, match="Private key or activation"):
        packaging.file_digest(private_key, audit=True)
    private_key.write_bytes(b"DUCAI-L3." + b"A" * 130)
    with pytest.raises(ValueError, match="Private key or activation"):
        packaging.file_digest(private_key, audit=True)
    owner = tmp_path / "issued-licenses.json"
    owner.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Owner/test/activation"):
        packaging.file_digest(owner, audit=True)


def test_test_manifest_verification_checks_entire_payload(packaging, tmp_path):
    package_root = tmp_path / "package"
    package_root.mkdir()
    payload = package_root / "payload.bin"
    payload.write_bytes(b"verified payload")
    manifest = {
        "schema": "ducai-rtx50-ocr-test-v1", "version": "2.0.0a153",
        "files_sha256": {"payload.bin": packaging.file_digest(payload)},
        "payload_files": 1, "payload_bytes": payload.stat().st_size,
    }
    (package_root / packaging.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert packaging.verify(package_root)["status"] == "PASS"
    payload.write_bytes(b"changed payload")
    with pytest.raises(ValueError, match="hash mismatch"):
        packaging.verify(package_root)
    extra = package_root / "extra.txt"
    extra.touch()
    with pytest.raises(ValueError, match="missing or unexpected"):
        packaging.verify(package_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows script host launcher contract")
@pytest.mark.parametrize("install_root,expected_code", [
    (r"E:\DUCAI-RTX50", 0),
    (r"E:\DUCAI RTX50", 0),
    (r"E:\Thử RTX50", 2),
    ("E:\\" + "nested-folder\\" * 15 + "DUCAI-RTX50", 3),
])
def test_test_launcher_rejects_unsupported_model_or_dll_paths(install_root, expected_code):
    launcher = (Path(__file__).parents[2] / "plans/261015-ocr-nvidia-recovery"
                / "test-package-files/Mo-ban-thu-RTX50.vbs")
    result = subprocess.run(
        [str(Path(os.environ["WINDIR"]) / "System32/cscript.exe"), "//NoLogo",
         str(launcher), "--check-path", install_root],
        capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == expected_code, result.stderr
    assert result.stdout.strip() == f"DUCAI_PATH_CHECK={expected_code}".encode("ascii")
