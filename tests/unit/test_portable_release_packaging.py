from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Windows portable packaging"
)


def run_packaging_functions(command: str, **variables: Path) -> None:
    script = PROJECT_ROOT / "scripts" / "build-portable-release.ps1"
    setup = """
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:TEST_PORTABLE_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst]
}, $true)
foreach ($function in $functions) {
    Invoke-Expression $function.Extent.Text
}
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", setup + command],
        env={
            **os.environ,
            "TEST_PORTABLE_SCRIPT": str(script),
            **{name: str(path) for name, path in variables.items()},
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_portable_gui_copy_preserves_hosts_without_application_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "release"
    target = tmp_path / "portable"
    (source / "engine_hosts").mkdir(parents=True)
    (source / "src").mkdir()
    (source / "src" / "private.py").write_bytes(b"private source")
    (source / "ducai_studio.whl").write_bytes(b"private wheel")
    (source / "engine_hosts" / "ocr_engine_host.py").write_bytes(b"private source")
    (source / "engine_hosts" / "ocr_engine_host.pyc").write_bytes(b"compiled host")
    (source / "DucaiStudio2.exe").write_bytes(b"binary fixture")
    run_packaging_functions(
        """
Copy-Tree $env:TEST_SOURCE $env:TEST_TARGET -KeepBytecode `
    -ExtraExcluded @('src', 'DUCAI_STUDIO_DATA') `
    -ExtraExcludedFiles @(
        '*.py', '*.pyw', '*.whl', '*.pem', '*.key', '.env', '.env.*', 'manifest.json')
exit 0
""",
        TEST_SOURCE=source,
        TEST_TARGET=target,
    )
    assert (target / "DucaiStudio2.exe").is_file()
    assert (target / "engine_hosts" / "ocr_engine_host.pyc").read_bytes() == b"compiled host"
    assert not (target / "src").exists()
    assert not list(target.rglob("*.py"))
    assert not list(target.rglob("*.whl"))


def test_external_runtime_copy_keeps_library_source_and_removes_cached_bytecode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runtime"
    target = tmp_path / "portable-runtime"
    source.mkdir()
    (source / "third_party.py").write_bytes(b"runtime library")
    (source / "third_party.pyc").write_bytes(b"machine cache")
    run_packaging_functions(
        "Copy-Tree $env:TEST_SOURCE $env:TEST_TARGET\nexit 0\n",
        TEST_SOURCE=source,
        TEST_TARGET=target,
    )
    assert (target / "third_party.py").is_file()
    assert not (target / "third_party.pyc").exists()


def test_portable_whisper_metadata_uses_relative_model_location(tmp_path: Path) -> None:
    metadata_file = tmp_path / "whisper_runtime" / "ducai_runtime.json"
    metadata_file.parent.mkdir()
    metadata_file.write_text(
        json.dumps(
            {
                "runtime_root": "C:/packager/runtime",
                "model_path": "C:/packager/model",
                "version": "1.2.1",
            }
        ),
        encoding="utf-8",
    )
    run_packaging_functions(
        "Rewrite-RuntimeMetadata $env:TEST_METADATA\n",
        TEST_METADATA=metadata_file,
    )
    metadata = json.loads(metadata_file.read_text(encoding="utf-8-sig"))
    assert "runtime_root" not in metadata
    assert metadata["model_path"] == "model_cache/whisper/Systran--faster-whisper-large-v3"
    assert metadata["version"] == "1.2.1"


def test_portable_archive_arguments_preserve_default_and_accept_stronger_compression() -> None:
    run_packaging_functions(
        """
$default = @(Get-PortableArchiveArguments)
if (($default -join ',') -ne 'a,-t7z,-mx=3,-mmt=4,-y') {
    throw 'Existing default compression changed'
}
$strong = @(Get-PortableArchiveArguments -CompressionLevel 9 -VolumeSize '1900m')
if (($strong -join ',') -ne 'a,-t7z,-mx=9,-mmt=4,-y,-v1900m') {
    throw 'Strong multipart arguments are incorrect'
}
"""
    )


@pytest.mark.parametrize(
    "arguments",
    [
        "-CompressionLevel -1",
        "-CompressionLevel 10",
        "-VolumeSize '0m'",
        "-VolumeSize '-y'",
        "-VolumeSize '1900m -psecret'",
        "-VolumeSize '1.5g'",
    ],
)
def test_portable_archive_arguments_reject_invalid_options(arguments: str) -> None:
    run_packaging_functions(
        f"""
$rejected = $false
try {{ Get-PortableArchiveArguments {arguments} | Out-Null }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'Invalid archive options were accepted' }}
"""
    )


@pytest.mark.parametrize("compression_level", [3, 9])
def test_real_portable_archive_roundtrip_is_lossless_with_unicode_names(
    tmp_path: Path, compression_level: int,
) -> None:
    seven_zip = shutil.which("7z")
    if not seven_zip:
        candidate = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "7-Zip/7z.exe"
        if candidate.is_file():
            seven_zip = str(candidate)
    if not seven_zip:
        pytest.skip("Real 7-Zip is required for the archive roundtrip")
    source = tmp_path / "full-package"
    source.mkdir()
    (source / "runtime").mkdir()
    expected = {
        "runtime/model.bin": bytes(range(256)) * 4096,
        "runtime/giọng Trúc Ly.txt": "Giọng nói và OCR được giữ nguyên.\n".encode(),
        "empty-file": b"",
    }
    for relative, content in expected.items():
        (source / relative).write_bytes(content)
    archive = tmp_path / "full.7z"
    restored = tmp_path / "extracted"
    run_packaging_functions(
        f"""
$arguments = @(Get-PortableArchiveArguments -CompressionLevel {compression_level})
& $env:TEST_SEVEN_ZIP @arguments $env:TEST_ARCHIVE $env:TEST_SOURCE
if ($LASTEXITCODE -ne 0) {{ throw 'Compression failed' }}
& $env:TEST_SEVEN_ZIP t $env:TEST_ARCHIVE
if ($LASTEXITCODE -ne 0) {{ throw 'Archive integrity failed' }}
& $env:TEST_SEVEN_ZIP x $env:TEST_ARCHIVE "-o$env:TEST_RESTORED" '-y'
if ($LASTEXITCODE -ne 0) {{ throw 'Extraction failed' }}
exit 0
""",
        TEST_SEVEN_ZIP=Path(seven_zip),
        TEST_SOURCE=source,
        TEST_ARCHIVE=archive,
        TEST_RESTORED=restored,
    )
    extracted = restored / source.name
    actual = {
        path.relative_to(extracted).as_posix(): path.read_bytes()
        for path in extracted.rglob("*") if path.is_file()
    }
    assert actual == expected
