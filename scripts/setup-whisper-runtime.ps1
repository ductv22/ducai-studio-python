param(
    [string]$DataRoot = "",
    [string]$Python = "",
    [switch]$SkipModelDownload
)

$ErrorActionPreference = "Stop"

if (-not $DataRoot) {
    $anchor = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $workspace = Get-Item -LiteralPath $anchor
    while ($null -ne $workspace -and $workspace.Name -ne "create-a-scheduled-task-called-weekday") {
        $workspace = $workspace.Parent
    }
    if ($null -eq $workspace) {
        throw "Không tìm thấy workspace để xác định DUCAI_STUDIO_DATA."
    }
    $DataRoot = Join-Path $workspace.FullName "outputs\DUCAI_STUDIO_DATA"
}

$dataRootPath = [IO.Path]::GetFullPath($DataRoot)
$runtimeRoot = Join-Path $dataRootPath "whisper_runtime"
$venvRoot = Join-Path $runtimeRoot ".venv"
$runtimePython = Join-Path $venvRoot "Scripts\python.exe"
$modelRelativePath = "model_cache\whisper\Systran--faster-whisper-large-v3"
$modelPath = Join-Path $dataRootPath $modelRelativePath
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
        throw "Không thể tạo runtime Whisper."
    }
}

uv pip install --python $runtimePython `
    "faster-whisper==1.2.1" `
    "ctranslate2==4.8.1" `
    "nvidia-cublas-cu12==12.6.4.1" `
    "nvidia-cudnn-cu12==9.5.1.17"
if ($LASTEXITCODE -ne 0) {
    throw "Không thể cài runtime Whisper đã ghim phiên bản."
}

$versions = & $runtimePython -c @"
import importlib.metadata as m
print(m.version('faster-whisper'))
print(m.version('ctranslate2'))
"@
if ($LASTEXITCODE -ne 0) {
    throw "Không đọc được phiên bản runtime Whisper."
}
$versionRows = @($versions | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($versionRows.Count -lt 2 -or $versionRows[0] -ne "1.2.1" -or $versionRows[1] -ne "4.8.1") {
    throw "Runtime Whisper không đúng faster-whisper 1.2.1 / CTranslate2 4.8.1."
}

if (-not $SkipModelDownload) {
    New-Item -ItemType Directory -Path $modelPath -Force | Out-Null
    $downloadCode = @"
import sys
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='Systran/faster-whisper-large-v3',
    local_dir=sys.argv[1],
)
"@
    & $runtimePython -c $downloadCode $modelPath
    if ($LASTEXITCODE -ne 0) {
        throw "Không tải được model Whisper large-v3."
    }
}

foreach ($required in @("model.bin", "config.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $modelPath $required) -PathType Leaf)) {
        throw "Model Whisper chưa đầy đủ: thiếu $required."
    }
}

$metadata = [ordered]@{
    schema = "ducai-whisper-runtime-v1"
    version = "1.2.1"
    ctranslate2_version = "4.8.1"
    model_id = "large-v3"
    repository_id = "Systran/faster-whisper-large-v3"
    model_path = $modelRelativePath.Replace('\', '/')
    runtime_root = $runtimeRoot
    installed_at = [DateTimeOffset]::Now.ToString("o")
}
$metadata | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $runtimeRoot "ducai_runtime.json") -Encoding UTF8

Write-Output "WHISPER_PYTHON=$runtimePython"
Write-Output "WHISPER_MODEL=$modelPath"
