param(
    [string]$OutputRoot = "",
    [switch]$SkipTests,
    [switch]$Clean,
    [switch]$ReuseExistingDeployment
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\python.exe")).Path
$deploy = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\pyside6-deploy.exe")).Path
$qmllint = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\pyside6-qmllint.exe")).Path
$version = (& $python -c "from ducai_studio import __version__; print(__version__)" ).Trim()

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot "..\..\outputs"
}
$outputRootPath = [IO.Path]::GetFullPath($OutputRoot)
$releaseRoot = Join-Path $outputRootPath "DUCAI-STUDIO-$version-win64"

if (Test-Path -LiteralPath $releaseRoot) {
    if (-not $Clean) {
        throw "Release đã tồn tại. Dùng -Clean để tạo lại: $releaseRoot"
    }
    $resolvedRelease = (Resolve-Path -LiteralPath $releaseRoot).Path
    $resolvedOutput = [IO.Path]::GetFullPath($outputRootPath).TrimEnd('\') + '\'
    if (-not $resolvedRelease.StartsWith($resolvedOutput, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Từ chối xóa release nằm ngoài OutputRoot."
    }
    if (-not (Split-Path $resolvedRelease -Leaf).StartsWith("DUCAI-STUDIO-")) {
        throw "Từ chối xóa thư mục không có tên release DUCAI."
    }
    Remove-Item -LiteralPath $resolvedRelease -Recurse -Force
}

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        & $python -m ruff check src tests
        if ($LASTEXITCODE -ne 0) { throw "Ruff gate thất bại." }
        & $python -m compileall -q src tests
        if ($LASTEXITCODE -ne 0) { throw "Compile gate thất bại." }
        & $python -m pytest
        if ($LASTEXITCODE -ne 0) { throw "Pytest gate thất bại." }
        $qml = @("src/ducai_studio/ui/qml/Main.qml") +
            (Get-ChildItem src/ducai_studio/ui/qml/controls,src/ducai_studio/ui/qml/screens -Filter *.qml | ForEach-Object { $_.FullName })
        & $qmllint -I src/ducai_studio/ui/qml $qml
        if ($LASTEXITCODE -ne 0) { throw "QML lint gate thất bại." }
    }

    Remove-Item -LiteralPath (Join-Path $projectRoot "dist\DucaiStudio2.exe") -Force -ErrorAction SilentlyContinue
    uv build --wheel --out-dir (Join-Path $projectRoot "dist")
    if ($LASTEXITCODE -ne 0) { throw "Không build được wheel." }

    if (-not $ReuseExistingDeployment) {
        $nuitkaVersion = (& $python -c "import importlib.metadata as m; print(m.version('Nuitka'))" 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or $nuitkaVersion -ne "4.1.1") {
            uv pip install --python $python "Nuitka==4.1.1"
            if ($LASTEXITCODE -ne 0) { throw "Không cài được Nuitka 4.1.1 bằng uv." }
        }

        $env:PATH = "$(Join-Path $projectRoot '.venv\Scripts');$env:PATH"
        $env:PYTHONPATH = Join-Path $projectRoot "src"
        $env:NUITKA_ASSUME_YES_FOR_DOWNLOADS = "yes"
        & $deploy -f --name DucaiStudio2 --mode standalone --nuitka-version 4.1.1 --extra-ignore-dirs DucaiStudio2.dist,deployment --extra-modules QtMultimedia,QtNetwork src/ducai_studio/__main__.py
        if ($LASTEXITCODE -ne 0) { throw "pyside6-deploy thất bại." }
    }
    $deploymentRoot = Join-Path $projectRoot "deployment\DucaiStudio2.dist"
    $executablePath = Join-Path $deploymentRoot "DucaiStudio2.exe"
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        $executablePath = Join-Path $deploymentRoot "__main__.exe"
    }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "Không tìm thấy executable trong deployment\DucaiStudio2.dist sau deploy."
    }
    $executable = Get-Item -LiteralPath $executablePath

    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $executable.DirectoryName "*") -Destination $releaseRoot -Recurse
    $generatedMain = Join-Path $releaseRoot "__main__.exe"
    if (Test-Path -LiteralPath $generatedMain) {
        Move-Item -LiteralPath $generatedMain -Destination (Join-Path $releaseRoot "DucaiStudio2.exe")
    }

    # Qt loads multimedia backends dynamically, so Nuitka cannot infer these
    # files from imports alone. Keep the backend beside the other Qt plugins
    # and its FFmpeg runtime DLLs beside the executable where Windows resolves
    # dependent libraries.
    $pysideRoot = & $python -c "import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).resolve().parent)"
    if ($LASTEXITCODE -ne 0 -or -not $pysideRoot) {
        throw "Không xác định được thư mục runtime PySide6."
    }
    $multimediaPluginSource = Join-Path $pysideRoot.Trim() "plugins\multimedia"
    $multimediaPluginTarget = Join-Path $releaseRoot "PySide6\qt-plugins\multimedia"
    if (-not (Test-Path -LiteralPath $multimediaPluginSource -PathType Container)) {
        throw "Thiếu plugin Qt Multimedia trong môi trường build."
    }
    New-Item -ItemType Directory -Path $multimediaPluginTarget -Force | Out-Null
    Copy-Item -Path (Join-Path $multimediaPluginSource "*") -Destination $multimediaPluginTarget -Force
    foreach ($runtimeDll in @(
        "avcodec-61.dll",
        "avformat-61.dll",
        "avutil-59.dll",
        "swresample-5.dll",
        "swscale-8.dll"
    )) {
        $runtimePath = Join-Path $pysideRoot.Trim() $runtimeDll
        if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
            throw "Thiếu DLL Qt Multimedia: $runtimeDll"
        }
        Copy-Item -LiteralPath $runtimePath -Destination $releaseRoot -Force
    }
    $wheel = Get-ChildItem (Join-Path $projectRoot "dist") -File -Filter "ducai_studio-*.whl" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $wheel) { throw "Không tìm thấy wheel DUCAI STUDIO." }
    Copy-Item -LiteralPath $wheel.FullName -Destination (Join-Path $releaseRoot $wheel.Name)
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\user-guide.md") -Destination $releaseRoot
    $engineHostRoot = Join-Path $releaseRoot "engine_hosts"
    New-Item -ItemType Directory -Path $engineHostRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot "src\ducai_studio\engine_hosts\ocr_engine_host.py") -Destination $engineHostRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "src\ducai_studio\engine_hosts\tts_engine_host.py") -Destination $engineHostRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "src\ducai_studio\engine_hosts\vieneu_v330_optimization.py") -Destination $engineHostRoot

    @"
DUCAI_STUDIO_DATA nằm ngoài gói GUI để model/cache có thể cập nhật độc lập.
Đặt biến DUCAI_STUDIO_DATA_ROOT nếu muốn chuyển thư mục dữ liệu dùng chung.
Chạy Diagnostics trong tool trước khi sản xuất phim đầu tiên.
"@ | Set-Content -LiteralPath (Join-Path $releaseRoot "DUCAI_STUDIO_DATA.README.txt") -Encoding UTF8

    $files = Get-ChildItem $releaseRoot -Recurse -File | Sort-Object FullName
    $hashes = [ordered]@{}
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($releaseRoot, $file.FullName).Replace('\', '/')
        $hashes[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{
        schema = "ducai-release-manifest-v1"
        product = "DUCAI STUDIO 2"
        version = $version
        platform = "windows-x86_64"
        gui_package = "DucaiStudio2.exe"
        external_data_root = "DUCAI_STUDIO_DATA"
        managed_engines = @(
            "PaddleOCR external Python runtime",
            "Piper 1.6 runtime and ONNX voices",
            "VieNeu 3.3 runtime and clone profiles",
            "FFmpeg/ffprobe",
            "Chrome or Edge for Gemini Web"
        )
        files_sha256 = $hashes
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $releaseRoot "manifest.json") -Encoding UTF8
    Write-Output $releaseRoot
}
finally {
    Pop-Location
}
