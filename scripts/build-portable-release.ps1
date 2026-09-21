param(
    [string]$OutputRoot = "",
    [string]$SourceDataRoot = "",
    [switch]$SkipBuild,
    [switch]$SkipTests,
    [switch]$Clean,
    [switch]$IncludeWhisper,
    [switch]$CreateArchive
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\python.exe")).Path
$version = (& $python -c "from ducai_studio import __version__; print(__version__)").Trim()
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot "..\..\outputs"
}
if (-not $SourceDataRoot) {
    $SourceDataRoot = Join-Path ([IO.Path]::GetFullPath($OutputRoot)) "DUCAI_STUDIO_DATA"
}
$outputRootPath = [IO.Path]::GetFullPath($OutputRoot)
$sourceDataPath = [IO.Path]::GetFullPath($SourceDataRoot)
$releaseRoot = Join-Path $outputRootPath "DUCAI-STUDIO-$version-win64"
$portableRoot = Join-Path $outputRootPath "DUCAI-STUDIO-$version-PORTABLE-win64"
$portableData = Join-Path $portableRoot "DUCAI_STUDIO_DATA"

function Remove-OwnedOutput([string]$Path, [string]$Prefix) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $outputPrefix = $outputRootPath.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Từ chối xóa đường dẫn nằm ngoài OutputRoot: $resolved"
    }
    if (-not (Split-Path $resolved -Leaf).StartsWith($Prefix)) {
        throw "Từ chối xóa thư mục không thuộc gói portable DUCAI: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Copy-Tree([string]$Source, [string]$Destination, [string[]]$ExtraExcluded = @()) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Thiếu thư mục bắt buộc: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $excluded = @(
        "__pycache__", ".pytest_cache", ".ruff_cache", "tests", "test",
        "docs", "examples", "benchmarks", "benchmark", ".git"
    ) + $ExtraExcluded
    $arguments = @(
        $Source, $Destination, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XD"
    ) + $excluded + @("/XF", "*.pyc", "*.pyo", "*.pdb")
    & robocopy @arguments | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Không sao chép được $Source (robocopy $LASTEXITCODE)."
    }
}

function Copy-File([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Thiếu file bắt buộc: $Source"
    }
    $parent = Split-Path $Destination -Parent
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Read-VenvHome([string]$VenvRoot) {
    $config = Join-Path $VenvRoot "pyvenv.cfg"
    $line = Get-Content -LiteralPath $config | Where-Object { $_ -match '^\s*home\s*=' } |
        Select-Object -First 1
    if (-not $line) { throw "pyvenv.cfg không có Python home: $config" }
    $runtimeHome = ($line -split '=', 2)[1].Trim()
    return (Resolve-Path -LiteralPath $runtimeHome).Path
}

function Copy-SlimPythonHome([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($directory in @("DLLs", "Lib", "tcl")) {
        $candidate = Join-Path $Source $directory
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $extra = if ($directory -eq "Lib") { @("site-packages") } else { @() }
            Copy-Tree $candidate (Join-Path $Destination $directory) $extra
        }
    }
    $patterns = @(
        "python.exe", "pythonw.exe", "python*.dll", "vcruntime*.dll", "LICENSE.txt", "COPYING"
    )
    foreach ($pattern in $patterns) {
        Get-ChildItem -LiteralPath $Source -File -Filter $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Destination "python.exe"))) {
        throw "Python home tối giản không có python.exe: $Destination"
    }
}

function Set-VenvHome([string]$VenvRoot, [string]$PythonHome) {
    $config = Join-Path $VenvRoot "pyvenv.cfg"
    $lines = Get-Content -LiteralPath $config
    $updated = @()
    $replaced = $false
    # Do not publish the packager's absolute filesystem path. The GUI repairs
    # this marker to the sibling bundled Python home before any engine starts.
    $portableHomeMarker = "__DUCAI_PORTABLE_PYTHON_HOME__"
    foreach ($line in $lines) {
        if (-not $replaced -and $line -match '^\s*home\s*=') {
            $updated += "home = $portableHomeMarker"
            $replaced = $true
        } else {
            $updated += $line
        }
    }
    if (-not $replaced) { $updated = @("home = $portableHomeMarker") + $updated }
    $updated | Set-Content -LiteralPath $config -Encoding UTF8
    Get-ChildItem -LiteralPath $PythonHome -File | Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -or
        $_.Name -like "python*.dll" -or $_.Name -like "vcruntime*.dll"
    } | ForEach-Object {
        Copy-File $_.FullName (Join-Path $VenvRoot "Scripts\$($_.Name)")
    }
}

function Remove-MachineBoundVenvArtifacts([string]$VenvRoot) {
    Get-ChildItem -LiteralPath $VenvRoot -Recurse -Force -File -Filter "*.pyc" |
        Remove-Item -Force
    $scripts = Join-Path $VenvRoot "Scripts"
    # uv/pip console entry points (including extensionless shebang scripts)
    # embed the source interpreter path. DUCAI invokes engine modules through
    # the repaired Python launcher, so publish only the native runtime files.
    Get-ChildItem -LiteralPath $scripts -File |
        Where-Object {
            $_.Name -notin @("python.exe", "pythonw.exe") -and
            $_.Name -notlike "python*.dll" -and
            $_.Name -notlike "vcruntime*.dll"
        } |
        Remove-Item -Force
    foreach ($name in @(
        "activate", "activate.bat", "activate.csh", "activate.fish", "activate.nu",
        "Activate.ps1"
    )) {
        $candidate = Join-Path $scripts $name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Remove-Item -LiteralPath $candidate -Force
        }
    }
}

function Rewrite-RuntimeMetadata([string]$MetadataPath) {
    if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) { return }
    try {
        $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    } catch {
        throw "Metadata runtime không hợp lệ: $MetadataPath"
    }
    if ($metadata.PSObject.Properties.Name -contains "runtime_root") {
        $metadata.PSObject.Properties.Remove("runtime_root")
    }
    $metadata | ConvertTo-Json -Depth 12 |
        Set-Content -LiteralPath $MetadataPath -Encoding UTF8
}

if (-not (Test-Path -LiteralPath $sourceDataPath -PathType Container)) {
    throw "Không tìm thấy DUCAI_STUDIO_DATA nguồn: $sourceDataPath"
}
if (-not $SkipBuild) {
    $buildArguments = @{ OutputRoot = $outputRootPath; Clean = $true }
    if ($SkipTests) { $buildArguments.SkipTests = $true }
    & (Join-Path $PSScriptRoot "build-release.ps1") @buildArguments
}
if (-not (Test-Path -LiteralPath $releaseRoot -PathType Container)) {
    throw "Chưa có release GUI để đóng gói: $releaseRoot"
}
if (Test-Path -LiteralPath $portableRoot) {
    if (-not $Clean) { throw "Gói portable đã tồn tại. Dùng -Clean để tạo lại." }
    Remove-OwnedOutput $portableRoot "DUCAI-STUDIO-"
}

New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null
Copy-Tree $releaseRoot $portableRoot

$ffmpegCommand = Get-Command ffmpeg -ErrorAction Stop
$ffprobeCommand = Get-Command ffprobe -ErrorAction Stop
$ffmpegSource = (Get-Item -LiteralPath $ffmpegCommand.Source).Target
$ffprobeSource = (Get-Item -LiteralPath $ffprobeCommand.Source).Target
if (-not $ffmpegSource) { $ffmpegSource = $ffmpegCommand.Source }
if (-not $ffprobeSource) { $ffprobeSource = $ffprobeCommand.Source }
Copy-File $ffmpegSource (Join-Path $portableRoot "bin\ffmpeg.exe")
Copy-File $ffprobeSource (Join-Path $portableRoot "bin\ffprobe.exe")
$ffmpegBase = Split-Path (Split-Path $ffmpegSource -Parent) -Parent
foreach ($name in @("LICENSE", "README.txt")) {
    $source = Join-Path $ffmpegBase $name
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-File $source (Join-Path $portableRoot "bin\FFmpeg-$name")
    }
}

$runtimeNames = @("paddle_runtime", "piper_runtime", "vieneu_3_3_runtime")
if ($IncludeWhisper) { $runtimeNames += "whisper_runtime" }
foreach ($runtime in $runtimeNames) {
    $source = Join-Path $sourceDataPath $runtime
    $destination = Join-Path $portableData $runtime
    Copy-Tree (Join-Path $source ".venv") (Join-Path $destination ".venv")
    $metadata = Join-Path $source "ducai_runtime.json"
    if (Test-Path -LiteralPath $metadata -PathType Leaf) {
        Copy-File $metadata (Join-Path $destination "ducai_runtime.json")
    }
}

$piperVoice = "vi_VN-vais1000-medium.onnx"
Copy-File (Join-Path $sourceDataPath "piper_voices\$piperVoice") `
    (Join-Path $portableData "piper_voices\$piperVoice")
Copy-File (Join-Path $sourceDataPath "piper_voices\$piperVoice.json") `
    (Join-Path $portableData "piper_voices\$piperVoice.json")

$huggingFace = Join-Path $sourceDataPath "model_cache\huggingface"
Copy-Tree (Join-Path $huggingFace "hub") `
    (Join-Path $portableData "model_cache\huggingface\hub") `
    @(".locks", "models--Systran--faster-whisper-large-v3")
if (Test-Path -LiteralPath (Join-Path $huggingFace "modules") -PathType Container) {
    Copy-Tree (Join-Path $huggingFace "modules") `
        (Join-Path $portableData "model_cache\huggingface\modules")
}

$paddleModels = Join-Path $env:USERPROFILE ".paddlex\official_models"
foreach ($model in @(
    "PP-OCRv6_tiny_det", "PP-OCRv6_small_rec",
    "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"
)) {
    Copy-Tree (Join-Path $paddleModels $model) `
        (Join-Path $portableData "paddle_model_cache\official_models\$model")
}

$python311Source = Read-VenvHome (Join-Path $sourceDataPath "paddle_runtime\.venv")
$python312Source = Read-VenvHome (Join-Path $sourceDataPath "piper_runtime\.venv")
$python311Target = Join-Path $portableData "python_runtime_311"
$python312Target = Join-Path $portableData "python_runtime_312"
Copy-SlimPythonHome $python311Source $python311Target
Copy-SlimPythonHome $python312Source $python312Target
Set-VenvHome (Join-Path $portableData "paddle_runtime\.venv") $python311Target
Set-VenvHome (Join-Path $portableData "piper_runtime\.venv") $python312Target
Set-VenvHome (Join-Path $portableData "vieneu_3_3_runtime\.venv") $python312Target
if ($IncludeWhisper) {
    Set-VenvHome (Join-Path $portableData "whisper_runtime\.venv") $python312Target
    $whisperModel = Join-Path $sourceDataPath `
        "model_cache\whisper\Systran--faster-whisper-large-v3"
    Copy-Tree $whisperModel (Join-Path $portableData `
        "model_cache\whisper\Systran--faster-whisper-large-v3")
}
foreach ($runtime in $runtimeNames) {
    Remove-MachineBoundVenvArtifacts (Join-Path $portableData "$runtime\.venv")
    Rewrite-RuntimeMetadata (Join-Path $portableData "$runtime\ducai_runtime.json")
}

New-Item -ItemType Directory -Path (Join-Path $portableData "vieneu_profiles") -Force |
    Out-Null
@"
Thư mục này được để trống nhằm bảo vệ dữ liệu cá nhân.
Người nhận tự tạo hoặc chọn giọng mẫu VieNeu trong DUCAI STUDIO.
"@ | Set-Content -LiteralPath (Join-Path $portableData "vieneu_profiles\README.txt") -Encoding UTF8

@"
DUCAI STUDIO $version PORTABLE

1. Giải nén toàn bộ gói vào thư mục có quyền ghi trước khi chạy.
2. Mở DucaiStudio2.exe trực tiếp; không di chuyển riêng file EXE.
3. OCR, Piper, VieNeu và FFmpeg đã nằm trong gói.
   Whisper chỉ có trong gói khi manifest ghi whisper_included=true.
4. Máy nhận cần Windows 64-bit. GPU NVIDIA và driver tương thích CUDA 12.8
   được khuyến nghị cho VieNeu/Paddle GPU.
5. Gemini Web dùng Chrome hoặc Edge đã cài trên máy; API cần key riêng của người nhận.

Gói không chứa project, media, SRT, cache voice, cookie trình duyệt, API key,
profile đăng nhập hoặc profile clone giọng của người đóng gói.
"@ | Set-Content -LiteralPath (Join-Path $portableRoot "HUONG-DAN-PORTABLE.txt") -Encoding UTF8

$forbiddenDirectories = @(
    "voice_cue_cache", "export_cache", "ocr_jobs", "voice_previews",
    "gemini-web-profile", "gemini_web_profile"
)
foreach ($name in $forbiddenDirectories) {
    if (Test-Path -LiteralPath (Join-Path $portableData $name)) {
        throw "Gói portable chứa dữ liệu riêng tư bị cấm: $name"
    }
}
if (Get-ChildItem -LiteralPath (Join-Path $portableData "vieneu_profiles") -File -Filter "*.npz") {
    throw "Gói portable không được chứa profile clone VieNeu."
}

$criticalRoots = @(
    (Join-Path $portableRoot "DucaiStudio2.exe"),
    (Join-Path $portableRoot "bin\ffmpeg.exe"),
    (Join-Path $portableRoot "bin\ffprobe.exe"),
    (Join-Path $portableRoot "engine_hosts"),
    (Join-Path $portableData "paddle_runtime"),
    (Join-Path $portableData "piper_runtime"),
    (Join-Path $portableData "vieneu_3_3_runtime"),
    (Join-Path $portableData "python_runtime_311"),
    (Join-Path $portableData "python_runtime_312"),
    (Join-Path $portableData "piper_voices"),
    (Join-Path $portableData "model_cache"),
    (Join-Path $portableData "paddle_model_cache")
)
if ($IncludeWhisper) {
    $criticalRoots += (Join-Path $portableData "whisper_runtime")
    $criticalRoots += (Join-Path $portableData `
        "model_cache\whisper\Systran--faster-whisper-large-v3")
}
$hashes = [ordered]@{}
foreach ($root in $criticalRoots) {
    $files = if (Test-Path -LiteralPath $root -PathType Leaf) {
        @(Get-Item -LiteralPath $root)
    } else {
        @(Get-ChildItem -LiteralPath $root -Recurse -File | Sort-Object FullName)
    }
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($portableRoot, $file.FullName).Replace('\', '/')
        $hashes[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$allFiles = Get-ChildItem -LiteralPath $portableRoot -Recurse -File
$totalBytes = ($allFiles | Measure-Object Length -Sum).Sum
$portableManifestPath = Join-Path $portableRoot "portable-manifest.json"
$components = @(
    "DUCAI GUI", "PaddleOCR GPU", "Piper 1.6 + vi_VN-vais1000-medium",
    "VieNeu 3.3 Turbo + model/tokenizer", "FFmpeg 8.1.1"
)
if ($IncludeWhisper) {
    $components += "Whisper large-v3 + faster-whisper 1.2.1"
}
$manifest = [ordered]@{
    schema = "ducai-portable-manifest-v1"
    product = "DUCAI STUDIO 2"
    version = $version
    platform = "windows-x86_64"
    package_bytes = [long]$totalBytes
    package_files = $allFiles.Count + 1
    components = $components
    whisper_included = [bool]$IncludeWhisper
    privacy = [ordered]@{
        projects = $false
        media = $false
        voice_cache = $false
        browser_cookies = $false
        credentials = $false
        clone_profiles = $false
    }
    critical_files_sha256 = $hashes
}
for ($iteration = 0; $iteration -lt 4; $iteration++) {
    $manifest | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath $portableManifestPath -Encoding UTF8
    $actualBytes = (Get-ChildItem -LiteralPath $portableRoot -Recurse -File |
        Measure-Object Length -Sum).Sum
    if ([long]$manifest.package_bytes -eq [long]$actualBytes) { break }
    $manifest.package_bytes = [long]$actualBytes
}
$totalBytes = (Get-ChildItem -LiteralPath $portableRoot -Recurse -File |
    Measure-Object Length -Sum).Sum
if ([long]$manifest.package_bytes -ne [long]$totalBytes) {
    throw "Không thể ổn định tổng dung lượng trong portable-manifest.json."
}

if ($CreateArchive) {
    $sevenZipCommand = Get-Command 7z -ErrorAction SilentlyContinue
    $sevenZipPath = if ($sevenZipCommand) { $sevenZipCommand.Source } else { $null }
    if (-not $sevenZipPath) {
        foreach ($candidate in @(
            "$env:ProgramFiles\7-Zip\7z.exe",
            "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
        )) {
            if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                $sevenZipPath = $candidate
                break
            }
        }
    }
    if ($sevenZipPath) {
        # bsdtar 3.8.x on Windows can crash while archiving VieNeu's bundled
        # samples whose names contain Vietnamese characters. 7-Zip preserves
        # those paths and avoids publishing a silently truncated archive.
        $archive = "$portableRoot.7z"
        $staleArchive = "$portableRoot.tar.xz"
        if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
        Push-Location $outputRootPath
        try {
            & $sevenZipPath a -t7z -mx=3 -mmt=on -y $archive `
                (Split-Path $portableRoot -Leaf)
            if ($LASTEXITCODE -ne 0) { throw "Không nén được gói portable bằng 7-Zip." }
            if (Test-Path -LiteralPath $staleArchive) {
                Remove-Item -LiteralPath $staleArchive -Force
            }
        }
        finally {
            Pop-Location
        }
    } else {
        $archive = "$portableRoot.tar.xz"
        $staleArchive = "$portableRoot.7z"
        if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
        & tar -cJf $archive -C $outputRootPath (Split-Path $portableRoot -Leaf)
        if ($LASTEXITCODE -ne 0) { throw "Không nén được gói portable bằng tar.xz." }
        if (Test-Path -LiteralPath $staleArchive) {
            Remove-Item -LiteralPath $staleArchive -Force
        }
    }
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "ARCHIVE=$archive"
    Write-Output "ARCHIVE_SHA256=$archiveHash"
}
Write-Output "PORTABLE=$portableRoot"
Write-Output "PORTABLE_BYTES=$totalBytes"
