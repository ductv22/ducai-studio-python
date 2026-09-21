param(
    [string]$DataRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if (-not $DataRoot) {
    $anchor = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $workspace = Get-Item -LiteralPath $anchor
    while ($null -ne $workspace -and $workspace.Name -ne "create-a-scheduled-task-called-weekday") {
        $workspace = $workspace.Parent
    }
    if ($null -eq $workspace) {
        throw "Không tìm thấy thư mục workspace để xác định DUCAI_STUDIO_DATA."
    }
    $DataRoot = Join-Path $workspace.FullName "outputs\DUCAI_STUDIO_DATA"
}

$runtimeRoot = Join-Path ([IO.Path]::GetFullPath($DataRoot)) "piper_runtime"
$venvRoot = Join-Path $runtimeRoot ".venv"
$runtimePython = Join-Path $venvRoot "Scripts\python.exe"
if (-not $Python) {
    $Python = (Resolve-Path (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")).Path
}

if (-not (Test-Path -LiteralPath $runtimePython)) {
    uv venv $venvRoot --python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "Không thể tạo runtime Piper."
    }
}

uv pip install --python $runtimePython "piper-tts==1.6.0"
if ($LASTEXITCODE -ne 0) {
    throw "Không thể cài piper-tts 1.6.0."
}

$installedVersion = & $runtimePython -c "import importlib.metadata as m; print(m.version('piper-tts'))"
if ($LASTEXITCODE -ne 0 -or $installedVersion.Trim() -ne "1.6.0") {
    throw "Runtime Piper không đúng phiên bản 1.6.0."
}

$metadata = [ordered]@{
    schema = "ducai-piper-runtime-v1"
    version = "1.6.0"
    package = "piper-tts"
    license = "GPL-3.0-or-later"
    runtime_root = $runtimeRoot
    installed_at = [DateTimeOffset]::Now.ToString("o")
}
$runtimeMetadata = Join-Path $runtimeRoot "ducai_runtime.json"
$metadata | ConvertTo-Json | Set-Content -LiteralPath $runtimeMetadata -Encoding UTF8

Write-Output $runtimePython
