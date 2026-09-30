from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from ducai_studio.infrastructure.update_manifest import (
    PUBLIC_KEY_BYTES,
    UpdateManifest,
    verify_manifest,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_staged_update(
    *,
    manifest_path: str | Path,
    signature_path: str | Path,
    payload_root: str | Path,
    expected_manifest_sha256: str,
    public_key: bytes = PUBLIC_KEY_BYTES,
) -> UpdateManifest:
    """Re-authenticate a staged payload for the external helper boundary."""

    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    signature_file = Path(signature_path).expanduser().resolve(strict=True)
    payload = Path(payload_root).expanduser().resolve(strict=True)
    manifest_bytes = manifest_file.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
        raise ValueError("Manifest cập nhật đã thay đổi sau khi xác minh")
    manifest = verify_manifest(manifest_bytes, signature_file.read_bytes(), public_key)
    expected = {item.path: item for item in manifest.files}
    actual: set[str] = set()
    for path in payload.rglob("*"):
        if path.is_dir():
            continue
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Payload cập nhật chứa liên kết/reparse point")
        relative = path.relative_to(payload).as_posix()
        item = expected.get(relative)
        if item is None:
            raise ValueError(f"Payload có file ngoài manifest: {relative}")
        if path.stat().st_size != item.size or _sha256_file(path) != item.sha256:
            raise ValueError(f"File cập nhật không còn khớp manifest: {relative}")
        actual.add(relative)
    if actual != set(expected):
        raise ValueError("Payload cập nhật thiếu file trong manifest")
    return manifest

_HELPER_SCRIPT = r'''param(
    [Parameter(Mandatory=$true)][string]$RequestPath,
    [Parameter(Mandatory=$true)][string]$ManifestSha256,
    [Parameter(Mandatory=$true)][int]$ParentPid
)
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop

function Fail([string]$Message) { throw $Message }
function Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Is-Reparse([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $attributes = (Get-Item -LiteralPath $Path -Force).Attributes
    return [bool]($attributes -band [IO.FileAttributes]::ReparsePoint)
}
function Assert-Under([string]$Path, [string]$Root) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "Đường dẫn cập nhật nằm ngoài thư mục cho phép."
    }
    return $full
}
function Assert-Managed-Path([string]$Relative) {
    if (
        -not $Relative -or
        $Relative.StartsWith('/') -or
        $Relative.StartsWith('\') -or
        $Relative.Contains('\') -or
        $Relative.Contains(':')
    ) { Fail "Đường dẫn file cập nhật không hợp lệ." }
    $parts = @($Relative.Split('/'))
    $protected = @(
        'ducai_studio_data', 'bin', 'projects', 'profiles', 'media', 'models',
        'cache', 'caches', 'paddle_runtime', 'piper_runtime',
        'vieneu_3_8_runtime', 'python_runtime_311', 'python_runtime_312',
        'whisper_runtime', '.git'
    )
    if ($protected -contains $parts[0].ToLowerInvariant()) {
        Fail "Gói cập nhật cố ghi vào vùng dữ liệu được bảo vệ."
    }
    foreach ($part in $parts) {
        $unsafeEnding = $part.EndsWith(' ') -or $part.EndsWith('.')
        if (-not $part -or $part -eq '.' -or $part -eq '..' -or $unsafeEnding) {
            Fail "Tên file cập nhật không an toàn."
        }
    }
}
function Wait-Exit([int]$ProcessId, [int]$Seconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 200
    }
    Fail "DUCAI STUDIO chưa đóng để cài đặt bản cập nhật."
}
function Stop-Owned-Tree([int]$ProcessId) {
    if ($ProcessId -le 0) { return }
    if ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        & taskkill.exe /PID $ProcessId /T /F | Out-Null
    }
}

$requestFile = [IO.Path]::GetFullPath($RequestPath)
$request = Get-Content -LiteralPath $requestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$installRoot = [IO.Path]::GetFullPath([string]$request.install_root)
$payloadRoot = [IO.Path]::GetFullPath([string]$request.payload_root)
$manifestPath = [IO.Path]::GetFullPath([string]$request.manifest_path)
$signaturePath = [IO.Path]::GetFullPath([string]$request.signature_path)
$backupRoot = [IO.Path]::GetFullPath([string]$request.backup_root)
$healthPath = [IO.Path]::GetFullPath([string]$request.health_path)
$journalPath = [IO.Path]::GetFullPath([string]$request.journal_path)
$handoffRoot = Split-Path -Parent $requestFile
$updateRoot = Split-Path -Parent $handoffRoot
$cleanupRoot = ""
if ([string]$request.cleanup_root) {
    $candidate = Assert-Under ([string]$request.cleanup_root) $updateRoot
    $candidateParent = Split-Path -Parent $candidate
    if (
        -not (Split-Path -Leaf $candidate).StartsWith("candidate-") -or
        -not $candidateParent.Equals($updateRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Is-Reparse $candidate)
    ) { Fail "Thư mục dọn gói cập nhật không hợp lệ." }
    $cleanupRoot = $candidate
}

if ((Sha256 $manifestPath) -ne $ManifestSha256.ToLowerInvariant()) {
    Fail "Manifest cập nhật đã thay đổi sau khi được xác minh."
}
if (-not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
    Fail "Thiếu chữ ký manifest cập nhật."
}
foreach ($root in @($installRoot, $payloadRoot)) {
    if (Is-Reparse $root) { Fail "Không cài cập nhật qua thư mục liên kết/reparse point." }
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$files = @($manifest.files)
if ($files.Count -eq 0) { Fail "Manifest cập nhật không có file ứng dụng." }
if ([string]$manifest.version -ne [string]$request.version) {
    Fail "Phiên bản manifest không khớp yêu cầu cài đặt."
}

# The currently trusted packaged executable owns the pinned Ed25519 key. It
# authenticates the canonical manifest and exact staged file set before the
# running GUI exits; the helper then repeats digest and per-file checks during
# mutation to close the post-verification TOCTOU window.
$verifierArguments = @(
    "--verify-update-stage",
    ('"' + $manifestPath + '"'),
    "--signature", ('"' + $signaturePath + '"'),
    "--payload", ('"' + $payloadRoot + '"'),
    "--manifest-sha256", $ManifestSha256.ToLowerInvariant()
)
$trustedExe = Assert-Under `
    (Join-Path $installRoot ([string]$request.executable_name)) $installRoot
$verifier = Start-Process `
    -FilePath $trustedExe `
    -ArgumentList $verifierArguments `
    -WorkingDirectory $installRoot `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($verifier.ExitCode -ne 0) {
    Fail "EXE hiện tại từ chối chữ ký hoặc payload cập nhật."
}

Wait-Exit -ProcessId $ParentPid -Seconds 45
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$journal = [ordered]@{ state = "backing_up"; installed = @(); backed_up = @() }
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding UTF8
$process = $null
$pendingTarget = ""

try {
    foreach ($entry in $files) {
        $relative = [string]$entry.path
        Assert-Managed-Path $relative
        $source = Assert-Under (Join-Path $payloadRoot $relative) $payloadRoot
        $target = Assert-Under (Join-Path $installRoot $relative) $installRoot
        $backup = Assert-Under (Join-Path $backupRoot $relative) $backupRoot
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            Fail "Thiếu file cập nhật: $relative"
        }
        if ((Sha256 $source) -ne ([string]$entry.sha256).ToLowerInvariant()) {
            Fail "Sai SHA-256 file cập nhật: $relative"
        }
        $cursor = Split-Path -Parent $target
        while ($cursor -and $cursor.Length -ge $installRoot.Length) {
            if (Is-Reparse $cursor) { Fail "Đường dẫn đích chứa reparse point: $relative" }
            if ($cursor.Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
            $cursor = Split-Path -Parent $cursor
        }
        $cursor = Split-Path -Parent $source
        while ($cursor -and $cursor.Length -ge $payloadRoot.Length) {
            if (Is-Reparse $cursor) { Fail "Đường dẫn nguồn chứa reparse point: $relative" }
            if ($cursor.Equals($payloadRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
            $cursor = Split-Path -Parent $cursor
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        $pendingTarget = $target + ".ducai-update-new-" + [string]$request.nonce
        Remove-Item -LiteralPath $pendingTarget -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $source -Destination $pendingTarget -Force
        if ((Sha256 $pendingTarget) -ne ([string]$entry.sha256).ToLowerInvariant()) {
            Fail "File cập nhật thay đổi trong lúc chuẩn bị: $relative"
        }
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
            $journal.backed_up += $relative
        }
        Move-Item -LiteralPath $pendingTarget -Destination $target -Force
        $pendingTarget = ""
        $journal.installed += $relative
        $journal.state = "installing"
        $journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding UTF8
    }

    $exe = Assert-Under (Join-Path $installRoot ([string]$request.executable_name)) $installRoot
    # Start-Process joins ArgumentList with spaces, so quote the only value that
    # can contain spaces. The generated health path is a file, never a trailing
    # backslash, and Windows paths cannot contain a literal quote.
    $quotedHealthPath = '"' + $healthPath + '"'
    $arguments = @(
        "--update-health-path", $quotedHealthPath,
        "--update-health-nonce", [string]$request.nonce,
        "--update-health-version", [string]$request.version
    )
    $process = Start-Process `
        -FilePath $exe `
        -ArgumentList $arguments `
        -WorkingDirectory $installRoot `
        -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) { break }
        if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
            try {
                $health = Get-Content `
                    -LiteralPath $healthPath `
                    -Raw `
                    -Encoding UTF8 | ConvertFrom-Json
                $healthRoot = [IO.Path]::GetFullPath([string]$health.install_root)
                if (
                    [int]$health.pid -eq $process.Id -and
                    [string]$health.nonce -eq [string]$request.nonce -and
                    [string]$health.version -eq [string]$request.version -and
                    [bool]$health.backend_ready -and
                    $healthRoot.Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)
                ) { $healthy = $true; break }
            } catch { }
        }
        Start-Sleep -Milliseconds 250
        $process.Refresh()
    }
    if (-not $healthy) { Fail "Bản mới không xác nhận trạng thái sẵn sàng." }
    $journal.state = "healthy"
    $journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding UTF8
    Remove-Item -LiteralPath $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
    if ($cleanupRoot) {
        Remove-Item -LiteralPath $cleanupRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch {
    if ($pendingTarget) {
        Remove-Item -LiteralPath $pendingTarget -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $process) { Stop-Owned-Tree -ProcessId $process.Id }
    [array]::Reverse($journal.installed)
    foreach ($relative in $journal.installed) {
        $target = Assert-Under (Join-Path $installRoot $relative) $installRoot
        $backup = Assert-Under (Join-Path $backupRoot $relative) $backupRoot
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $backup -Destination $target -Force
        } else {
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        }
    }
    $journal.state = "rolled_back"
    $journal.error = $_.Exception.Message
    $journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding UTF8
    $oldExe = Join-Path $installRoot ([string]$request.executable_name)
    if (Test-Path -LiteralPath $oldExe -PathType Leaf) {
        Start-Process -FilePath $oldExe -WorkingDirectory $installRoot | Out-Null
    }
    if ($cleanupRoot) {
        Remove-Item -LiteralPath $cleanupRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
'''


@dataclass(frozen=True, slots=True)
class UpdateInstallHandoff:
    request_path: Path
    helper_path: Path
    manifest_sha256: str
    nonce: str


def prepare_update_handoff(
    *,
    install_root: str | Path,
    payload_root: str | Path,
    manifest_path: str | Path,
    signature_path: str | Path,
    update_root: str | Path,
    version: str,
    expected_manifest_sha256: str,
    executable_name: str = "DucaiStudio2.exe",
    public_key: bytes = PUBLIC_KEY_BYTES,
) -> UpdateInstallHandoff:
    """Create a private, explicit handoff for the out-of-process installer."""

    install = Path(install_root).expanduser().resolve()
    payload = Path(payload_root).expanduser().resolve()
    manifest = Path(manifest_path).expanduser().resolve()
    signature = Path(signature_path).expanduser().resolve()
    root = Path(update_root).expanduser().resolve()
    if (
        not install.is_dir()
        or not payload.is_dir()
        or not manifest.is_file()
        or not signature.is_file()
    ):
        raise ValueError("Thiếu thư mục, manifest hoặc chữ ký cập nhật đã xác minh")
    if not (install / executable_name).is_file():
        raise ValueError("Không tìm thấy DucaiStudio2.exe trong thư mục cài đặt")
    if install == payload or install in payload.parents or payload in install.parents:
        raise ValueError("Vùng tải cập nhật phải nằm ngoài thư mục cài đặt")
    verified = verify_staged_update(
        manifest_path=manifest,
        signature_path=signature,
        payload_root=payload,
        expected_manifest_sha256=expected_manifest_sha256,
        public_key=public_key,
    )
    if verified.version != version:
        raise ValueError("Phiên bản manifest không khớp bản cập nhật đã chọn")
    root.mkdir(parents=True, exist_ok=True)
    nonce = token_urlsafe(32)
    handoff_root = root / f"install-{nonce}"
    handoff_root.mkdir(parents=True, exist_ok=False)
    helper_path = handoff_root / "ducai-update-helper.ps1"
    request_path = handoff_root / "install-request.json"
    manifest_copy = handoff_root / "update-manifest.json"
    signature_copy = handoff_root / "update-manifest.sig"
    manifest_copy.write_bytes(manifest.read_bytes())
    signature_copy.write_bytes(signature.read_bytes())
    helper_path.write_text(_HELPER_SCRIPT, encoding="utf-8-sig")
    request: dict[str, Any] = {
        "schema": "ducai-update-install-v1",
        "install_root": str(install),
        "payload_root": str(payload),
        "manifest_path": str(manifest_copy),
        "signature_path": str(signature_copy),
        "backup_root": str(handoff_root / "backup"),
        "health_path": str(handoff_root / "health.json"),
        "journal_path": str(handoff_root / "journal.json"),
        "executable_name": executable_name,
        "version": version,
        "nonce": nonce,
        "cleanup_root": (
            str(payload.parent)
            if payload.name.casefold() == "payload"
            and payload.parent.name.casefold().startswith("candidate-")
            and payload.parent.parent == root
            else ""
        ),
    }
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest_copy.read_bytes()).hexdigest()
    if digest != expected_manifest_sha256:
        raise ValueError("Manifest thay đổi trong lúc tạo handoff cập nhật")
    return UpdateInstallHandoff(request_path, helper_path, digest, nonce)


def launch_update_helper(
    handoff: UpdateInstallHandoff,
    *,
    parent_pid: int | None = None,
) -> subprocess.Popen[bytes]:
    """Launch the trusted helper without inheriting a console window."""

    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(handoff.helper_path),
        "-RequestPath",
        str(handoff.request_path),
        "-ManifestSha256",
        handoff.manifest_sha256,
        "-ParentPid",
        str(parent_pid or os.getpid()),
    ]
    log_path = handoff.helper_path.parent / "helper.log"
    with log_path.open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=str(handoff.helper_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def current_install_root(executable_path: str | Path | None = None) -> Path:
    executable = Path(executable_path or sys.executable).expanduser().resolve()
    if executable.name.casefold() != "ducaistudio2.exe":
        raise RuntimeError("Tự cập nhật chỉ khả dụng trong bản DUCAI STUDIO đã đóng gói")
    return executable.parent
