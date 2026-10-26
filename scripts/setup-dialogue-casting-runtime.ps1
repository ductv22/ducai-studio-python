param(
    [string]$DataRoot = "",
    [string]$Python = "",
    [string]$DownloadCache = "",
    [switch]$Offline
)

$ErrorActionPreference = "Stop"

if (-not $DataRoot) {
    $workspace = Get-Item -LiteralPath (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    while ($null -ne $workspace -and $workspace.Name -ne "create-a-scheduled-task-called-weekday") {
        $workspace = $workspace.Parent
    }
    if ($null -ne $workspace) {
        $DataRoot = Join-Path $workspace.FullName "outputs\DUCAI_STUDIO_DATA"
    } else {
        $packageRoot = Split-Path $PSScriptRoot -Parent
        $portableData = Join-Path $packageRoot "DUCAI_STUDIO_DATA"
        $siblingData = Join-Path (Split-Path $packageRoot -Parent) "DUCAI_STUDIO_DATA"
        if ($env:DUCAI_STUDIO_DATA_ROOT) {
            $DataRoot = $env:DUCAI_STUDIO_DATA_ROOT
        } elseif (Test-Path -LiteralPath $portableData -PathType Container) {
            $DataRoot = $portableData
        } elseif (Test-Path -LiteralPath $siblingData -PathType Container) {
            $DataRoot = $siblingData
        } else {
            # A fresh portable setup creates its external data root here.
            $DataRoot = $portableData
        }
    }
}

$dataRootPath = [IO.Path]::GetFullPath($DataRoot)
$packagedUv = Join-Path $PSScriptRoot "uv.exe"
$uvCommand = if (Test-Path -LiteralPath $packagedUv -PathType Leaf) {
    (Resolve-Path -LiteralPath $packagedUv).Path
} else {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "Thiếu uv. Hãy dùng bộ cài đi kèm gói DUCAI hoặc cài uv." }
    $command.Source
}
if (-not $Python) {
    $repositoryPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    $bundledPython = Join-Path $dataRootPath "python_runtime_312\python.exe"
    if (Test-Path -LiteralPath $repositoryPython -PathType Leaf) {
        $Python = (Resolve-Path -LiteralPath $repositoryPython).Path
    } elseif (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        $Python = (Resolve-Path -LiteralPath $bundledPython).Path
    } else {
        throw "Thiếu Python 3.12 dùng để tạo runtime phân vai thoại."
    }
}
$runtimeRoot = Join-Path $dataRootPath "dialogue_casting_runtime"
$venvRoot = Join-Path $runtimeRoot ".venv"
$runtimePython = Join-Path $venvRoot "Scripts\python.exe"
$classifierPython = Join-Path $dataRootPath `
    "vieneu_3_8_runtime\.venv\Scripts\python.exe"
$models = Join-Path $runtimeRoot "models"
$ecapaModels = Join-Path $runtimeRoot "ecapa-experiment"
$requirements = Join-Path $PSScriptRoot "dialogue_casting\requirements.txt"
$lab = Join-Path $PSScriptRoot "dialogue-casting.pyc"
if (-not (Test-Path -LiteralPath $lab -PathType Leaf)) {
    $lab = Join-Path $PSScriptRoot "dialogue-casting.py"
}
if (-not $DownloadCache) { $DownloadCache = Join-Path $runtimeRoot "downloads" }
$offlineArgs = @()
if ($Offline) { $offlineArgs = @("--offline") }

if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    & $uvCommand venv $venvRoot --python $Python --no-python-downloads @offlineArgs
    if ($LASTEXITCODE -ne 0) { throw "Không tạo được runtime phân vai thoại Python 3.12." }
}
& $uvCommand pip sync --python $runtimePython --require-hashes --only-binary :all: $requirements @offlineArgs
if ($LASTEXITCODE -ne 0) { throw "Không cài được các dependency đã ghim checksum." }

if (-not (Test-Path -LiteralPath $models -PathType Container)) {
    & $runtimePython $lab install-models --model-root $models --download-cache $DownloadCache @offlineArgs
    if ($LASTEXITCODE -ne 0) { throw "Cài bộ nhận diện thất bại. Xem lỗi model/checksum ở trên." }
}
& $runtimePython $lab verify --model-root $models
if ($LASTEXITCODE -ne 0) { throw "Bộ nhận diện chưa qua kiểm tra offline." }
if (-not (Test-Path -LiteralPath $classifierPython -PathType Leaf)) {
    throw "Thiếu VieNeu Python runtime dùng chung cho classifier PyTorch: $classifierPython. Cài runtime VieNeu 3.8 trước."
}
$ecapaCache = Join-Path $DownloadCache "ecapa"
if (-not (Test-Path -LiteralPath $ecapaModels -PathType Container)) {
    & $classifierPython $lab install-ecapa-model --model-root $ecapaModels `
        --download-cache $ecapaCache @offlineArgs
    if ($LASTEXITCODE -ne 0) { throw "Cài model phân loại ECAPA thất bại; kiểm tra URL và checksum ở trên." }
}
& $classifierPython $lab verify-ecapa --model-root $ecapaModels
if ($LASTEXITCODE -ne 0) { throw "Model/runtime ECAPA chưa qua kiểm tra offline." }
$mossSetup = Join-Path $PSScriptRoot "setup-dialogue-casting-moss.ps1"
& $mossSetup -DataRoot $DataRoot -Python $Python -DownloadCache (Join-Path $DownloadCache "moss") -Offline:$Offline
if ($LASTEXITCODE -ne 0) { throw "Bộ MOSS chưa qua kiểm tra cài đặt." }
Write-Output "CASTING_PYTHON=$runtimePython"
Write-Output "CASTING_CLASSIFIER_PYTHON=$classifierPython"
Write-Output "CASTING_MODELS=$models"
Write-Output "CASTING_ECAPA_MODELS=$ecapaModels"
Write-Output "Bộ thử nghiệm đã cài. Chưa được nghiệm thu độ chính xác cho hoạt hình Trung Quốc."
