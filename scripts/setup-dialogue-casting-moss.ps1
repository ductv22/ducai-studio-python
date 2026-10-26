param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$Python = "",
    [string]$DownloadCache = "",
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
$dataRootPath = [IO.Path]::GetFullPath($DataRoot)

function Resolve-SetupPython([string]$RequestedPython) {
    if ($RequestedPython) {
        return (Resolve-Path -LiteralPath $RequestedPython).Path
    }
    $repositoryPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $repositoryPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $repositoryPython).Path
    }
    $bundledPython = Join-Path $dataRootPath "python_runtime_312\python.exe"
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $bundledPython).Path
    }
    throw "Thiếu Python 3.12 để cài MOSS. Gói portable cần DUCAI_STUDIO_DATA\python_runtime_312\python.exe."
}

function Resolve-UvCommand {
    $packagedUv = Join-Path $PSScriptRoot "uv.exe"
    if (Test-Path -LiteralPath $packagedUv -PathType Leaf) {
        return (Resolve-Path -LiteralPath $packagedUv).Path
    }
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Thiếu uv để đồng bộ dependency MOSS theo checksum."
    }
    return $command.Source
}

$Python = Resolve-SetupPython $Python
$uvCommand = Resolve-UvCommand
$castingRoot = Join-Path $dataRootPath "dialogue_casting_runtime"
$mossRoot = Join-Path $castingRoot "moss"
$legacyRoot = Join-Path $castingRoot "moss-experiment"
$mossPython = Join-Path $mossRoot ".venv\Scripts\python.exe"
$legacyPython = Join-Path $legacyRoot ".venv\Scripts\python.exe"
$syncMarker = Join-Path $mossRoot ".dependency-sync-complete"
$requirements = Join-Path $PSScriptRoot "dialogue_casting\moss-requirements.txt"
$installer = Join-Path $PSScriptRoot "dialogue-casting-moss.pyc"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    $installer = Join-Path $PSScriptRoot "dialogue-casting-moss.py"
}
foreach ($requiredFile in @($requirements, $installer)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Bộ cài MOSS thiếu file bắt buộc: $requiredFile"
    }
}
$offlineArgs = @()
if ($Offline) { $offlineArgs = @("--offline") }

function Copy-VerifiedLegacyEnvironment {
    $legacyVenv = Join-Path $legacyRoot ".venv"
    $canonicalVenv = Join-Path $mossRoot ".venv"
    if (-not (Test-Path -LiteralPath $legacyPython -PathType Leaf)) { return $false }
    $verification = & $legacyPython -X utf8 $installer verify --runtime-root $legacyRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Runtime MOSS cũ không qua xác minh; bộ cài sẽ dùng cache hoặc tải bản đã ghim."
        return $false
    }
    $verification | ForEach-Object { Write-Host $_ }
    New-Item -ItemType Directory -Path $mossRoot -Force | Out-Null
    if (Test-Path -LiteralPath $canonicalVenv) {
        $resolvedVenv = (Resolve-Path -LiteralPath $canonicalVenv).Path
        $expectedVenv = [IO.Path]::GetFullPath((Join-Path $mossRoot ".venv"))
        if (-not $resolvedVenv.Equals($expectedVenv, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Từ chối thay runtime ngoài thư mục MOSS chuẩn: $resolvedVenv"
        }
        Remove-Item -LiteralPath $canonicalVenv -Recurse -Force
    }
    & robocopy $legacyVenv $canonicalVenv /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Không sao chép được môi trường MOSS đã xác minh." }
    return $true
}

$seededFromLegacy = $false
if (-not (Test-Path -LiteralPath $syncMarker -PathType Leaf) -and
    (Test-Path -LiteralPath $legacyPython -PathType Leaf)) {
    $seededFromLegacy = Copy-VerifiedLegacyEnvironment
}
if (-not (Test-Path -LiteralPath $mossPython -PathType Leaf)) {
    & $uvCommand venv (Join-Path $mossRoot ".venv") --python $Python --no-python-downloads @offlineArgs
    if ($LASTEXITCODE -ne 0) { throw "Không tạo được runtime MOSS chuẩn tại $mossRoot." }
}

# Always synchronize the canonical environment. An existing executable is not
# proof that its packages still match the reviewed, hashed dependency set.
& $uvCommand pip sync --python $mossPython --require-hashes --only-binary :all: `
    --extra-index-url https://download.pytorch.org/whl/cu128 `
    --index-strategy unsafe-best-match $requirements @offlineArgs
if ($LASTEXITCODE -ne 0 -and -not $seededFromLegacy -and
    (Test-Path -LiteralPath $legacyPython -PathType Leaf)) {
    $seededFromLegacy = Copy-VerifiedLegacyEnvironment
    & $uvCommand pip sync --python $mossPython --require-hashes --only-binary :all: `
        --extra-index-url https://download.pytorch.org/whl/cu128 `
        --index-strategy unsafe-best-match $requirements @offlineArgs
}
if ($LASTEXITCODE -ne 0) { throw "Không đồng bộ được dependency MOSS đã ghim checksum." }
Set-Content -LiteralPath $syncMarker -Value "moss-requirements.txt" -Encoding ascii

if (-not $DownloadCache) { $DownloadCache = Join-Path $mossRoot "downloads" }
$legacyArgs = @()
if (Test-Path -LiteralPath $legacyRoot -PathType Container) {
    $canonicalVerification = & $mossPython -X utf8 $installer verify --runtime-root $mossRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        $legacyVerification = & $mossPython -X utf8 $installer verify --runtime-root $legacyRoot 2>&1
        if ($LASTEXITCODE -eq 0) {
            $legacyArgs = @("--legacy-root", $legacyRoot)
        } else {
            Write-Warning "Bỏ qua bộ MOSS cũ thiếu hoặc sai checksum; dùng cache hoặc tải bản đã ghim."
        }
    }
}
& $mossPython -X utf8 $installer install --runtime-root $mossRoot `
    --cache $DownloadCache @legacyArgs @offlineArgs
if ($LASTEXITCODE -ne 0) { throw "Cài hoặc xác minh MOSS thất bại; xem lỗi model/checksum ở trên." }
Write-Output "CASTING_MOSS_PYTHON=$mossPython"
Write-Output "CASTING_MOSS_ROOT=$mossRoot"
