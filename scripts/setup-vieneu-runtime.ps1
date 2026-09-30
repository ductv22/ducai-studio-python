param(
    [string]$DataRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if (-not $DataRoot) {
    if ($env:DUCAI_STUDIO_DATA_ROOT) {
        $DataRoot = $env:DUCAI_STUDIO_DATA_ROOT
    }
    else {
        $anchor = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
        $workspace = Get-Item -LiteralPath $anchor
        while ($null -ne $workspace -and $workspace.Name -ne "create-a-scheduled-task-called-weekday") {
            $workspace = $workspace.Parent
        }
        if ($null -ne $workspace) {
            $DataRoot = Join-Path $workspace.FullName "outputs\DUCAI_STUDIO_DATA"
        }
        else {
            $DataRoot = Join-Path $env:LOCALAPPDATA "DUCAI STUDIO\DUCAI_STUDIO_DATA"
        }
    }
}

$dataRootPath = [IO.Path]::GetFullPath($DataRoot)
$runtimeRoot = Join-Path $dataRootPath "vieneu_3_8_runtime"
$venvRoot = Join-Path $runtimeRoot ".venv"
$runtimePython = Join-Path $venvRoot "Scripts\python.exe"
if (-not $Python) {
    $Python = (Resolve-Path (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")).Path
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Thiếu uv. Hãy cài uv rồi chạy lại script."
}
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    uv venv $venvRoot --python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "Không thể tạo runtime VieNeu 3.8.1."
    }
}

uv pip install --python $runtimePython `
    --index-url "https://download.pytorch.org/whl/cu128" `
    "torch==2.8.0" `
    "torchaudio==2.8.0"
if ($LASTEXITCODE -ne 0) {
    throw "Không thể cài PyTorch CUDA 12.8 cho VieNeu."
}

uv pip install --python $runtimePython `
    "vieneu[cuda]==3.8.1" `
    "transformers==4.57.6"
if ($LASTEXITCODE -ne 0) {
    throw "Không thể cài VieNeu 3.8.1 đã ghim phiên bản."
}

$verificationCode = @"
import importlib.metadata as metadata
import json
import torch

result = {
    'vieneu': metadata.version('vieneu'),
    'torch': metadata.version('torch'),
    'torchaudio': metadata.version('torchaudio'),
    'transformers': metadata.version('transformers'),
    'cuda_available': torch.cuda.is_available(),
    'cuda_version': torch.version.cuda or '',
    'device_name': torch.cuda.get_device_name(0) if torch.cuda.is_available() else '',
}
print(json.dumps(result, ensure_ascii=False))
"@
$verificationJson = & $runtimePython -c $verificationCode
if ($LASTEXITCODE -ne 0) {
    throw "Không thể kiểm tra runtime VieNeu 3.8.1."
}
$verification = $verificationJson | ConvertFrom-Json
if ($verification.vieneu -ne "3.8.1") {
    throw "Runtime VieNeu không đúng phiên bản 3.8.1."
}
if (-not $verification.cuda_available -or $verification.cuda_version -ne "12.8") {
    throw "VieNeu 3.8.1 không nhận được CUDA 12.8."
}

$metadata = [ordered]@{
    schema = "ducai-vieneu-runtime-v2"
    version = "3.8.1"
    package = "vieneu"
    mode = "v3turbo"
    source = "https://github.com/pnnbao97/VieNeu-TTS"
    tag = "v3.8.1"
    model_repository = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
    torch_version = $verification.torch
    cuda_version = $verification.cuda_version
    device_name = $verification.device_name
    runtime_root = $runtimeRoot
    installed_at = [DateTimeOffset]::Now.ToString("o")
}
$metadata | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $runtimeRoot "ducai_runtime.json") -Encoding UTF8

Write-Output "VIENEU_PYTHON=$runtimePython"
Write-Output "VIENEU_VERSION=$($verification.vieneu)"
Write-Output "VIENEU_DEVICE=$($verification.device_name)"
