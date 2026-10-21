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
        throw "Release already exists. Use -Clean to rebuild: $releaseRoot"
    }
    $resolvedRelease = (Resolve-Path -LiteralPath $releaseRoot).Path
    $resolvedOutput = [IO.Path]::GetFullPath($outputRootPath).TrimEnd('\') + '\'
    if (-not $resolvedRelease.StartsWith($resolvedOutput, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a release outside OutputRoot."
    }
    if (-not (Split-Path $resolvedRelease -Leaf).StartsWith("DUCAI-STUDIO-")) {
        throw "Refusing to remove a directory without a DUCAI release name."
    }
    Remove-Item -LiteralPath $resolvedRelease -Recurse -Force
}

Push-Location $projectRoot
try {
    & $python (Join-Path $PSScriptRoot "check-license-release.py")
    if ($LASTEXITCODE -ne 0) { throw "Licensing trust root validation failed." }
    if (-not $SkipTests) {
        & $python -m ruff check src tests
        if ($LASTEXITCODE -ne 0) { throw "Ruff gate failed." }
        & $python -m compileall -q src tests
        if ($LASTEXITCODE -ne 0) { throw "Compile gate failed." }
        & $python -m pytest
        if ($LASTEXITCODE -ne 0) { throw "Pytest gate failed." }
        $qml = @("src/ducai_studio/ui/qml/Main.qml") +
            (Get-ChildItem src/ducai_studio/ui/qml/controls,src/ducai_studio/ui/qml/screens -Filter *.qml | ForEach-Object { $_.FullName })
        & $qmllint -I src/ducai_studio/ui/qml $qml
        if ($LASTEXITCODE -ne 0) { throw "QML lint gate failed." }
    }

    Remove-Item -LiteralPath (Join-Path $projectRoot "dist\DucaiStudio2.exe") -Force -ErrorAction SilentlyContinue
    uv build --wheel --out-dir (Join-Path $projectRoot "dist")
    if ($LASTEXITCODE -ne 0) { throw "Wheel build failed." }

    $deploymentRoot = Join-Path $projectRoot "deployment\DucaiStudio2.dist"
    $deploymentIdentity = Join-Path $deploymentRoot "DUCAI_BUILD_VERSION"
    $executablePath = ""
    if ($ReuseExistingDeployment) {
        if (
            -not (Test-Path -LiteralPath $deploymentIdentity -PathType Leaf) -or
            (Get-Content -LiteralPath $deploymentIdentity -Raw).Trim() -ne $version
        ) {
            throw "Existing deployment was built for another or unknown version; rebuild it."
        }
    }
    if (-not $ReuseExistingDeployment) {
        $nuitkaVersion = (& $python -c "import importlib.metadata as m; print(m.version('Nuitka'))" 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or $nuitkaVersion -ne "4.1.1") {
            uv pip install --python $python "Nuitka==4.1.1"
            if ($LASTEXITCODE -ne 0) { throw "Could not install Nuitka 4.1.1 with uv." }
        }

        $env:PATH = "$(Join-Path $projectRoot '.venv\Scripts');$env:PATH"
        $env:PYTHONPATH = Join-Path $projectRoot "src"
        $env:NUITKA_ASSUME_YES_FOR_DOWNLOADS = "yes"
        $buildStartedUtc = [DateTime]::UtcNow
        & $deploy -f --name DucaiStudio2 --mode standalone --nuitka-version 4.1.1 --extra-ignore-dirs DucaiStudio2.dist,deployment --extra-modules QtMultimedia,QtNetwork src/ducai_studio/__main__.py
        if ($LASTEXITCODE -ne 0) { throw "pyside6-deploy failed." }
        if (-not (Test-Path -LiteralPath $deploymentRoot -PathType Container)) {
            throw "Deployment directory is missing after pyside6-deploy."
        }
        # Some deploy versions swallow compiler failures. Never label the
        # previous deployment with the new version when no fresh EXE exists.
        $freshExecutable = @("__main__.exe", "DucaiStudio2.exe") |
            ForEach-Object { Get-Item -LiteralPath (Join-Path $deploymentRoot $_) -ErrorAction SilentlyContinue } |
            Where-Object { $_.LastWriteTimeUtc -ge $buildStartedUtc } |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($null -eq $freshExecutable) {
            throw "No freshly compiled executable; refusing to relabel an old deployment."
        }
        $executablePath = $freshExecutable.FullName
        Set-Content -LiteralPath $deploymentIdentity -Value $version -Encoding ascii
    }
    if (-not $executablePath) {
        $executablePath = Join-Path $deploymentRoot "__main__.exe"
        if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
            $executablePath = Join-Path $deploymentRoot "DucaiStudio2.exe"
        }
    }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "Executable was not found in deployment\DucaiStudio2.dist after deploy."
    }
    $executable = Get-Item -LiteralPath $executablePath

    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $executable.DirectoryName "*") -Destination $releaseRoot -Recurse
    Copy-Item -LiteralPath $executable.FullName -Destination (Join-Path $releaseRoot "DucaiStudio2.exe") -Force
    $generatedMain = Join-Path $releaseRoot "__main__.exe"
    if (Test-Path -LiteralPath $generatedMain) {
        Remove-Item -LiteralPath $generatedMain -Force
    }

    # Qt loads multimedia backends dynamically, so Nuitka cannot infer these
    # files from imports alone. Keep the backend beside the other Qt plugins
    # and its FFmpeg runtime DLLs beside the executable where Windows resolves
    # dependent libraries.
    $pysideRoot = & $python -c "import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).resolve().parent)"
    if ($LASTEXITCODE -ne 0 -or -not $pysideRoot) {
        throw "Could not locate the PySide6 runtime directory."
    }
    $multimediaPluginSource = Join-Path $pysideRoot.Trim() "plugins\multimedia"
    $multimediaPluginTarget = Join-Path $releaseRoot "PySide6\qt-plugins\multimedia"
    if (-not (Test-Path -LiteralPath $multimediaPluginSource -PathType Container)) {
        throw "Qt Multimedia plugin is missing from the build environment."
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
            throw "Qt Multimedia DLL is missing: $runtimeDll"
        }
        Copy-Item -LiteralPath $runtimePath -Destination $releaseRoot -Force
    }
    $wheel = Get-ChildItem (Join-Path $projectRoot "dist") -File -Filter "ducai_studio-*.whl" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $wheel) { throw "DUCAI STUDIO wheel was not found." }
    # The customer runtime is compiled; never distribute the editable source wheel.
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\user-guide.md") -Destination $releaseRoot
    # QML is loaded at runtime, not compiled into the executable. Refresh it
    # from the tested source even when the matching compiled deployment is reused.
    $qmlSource = Join-Path $projectRoot "src\ducai_studio\ui\qml"
    $qmlTarget = Join-Path $releaseRoot "ui\qml"
    if (-not (Test-Path -LiteralPath (Join-Path $qmlSource "Main.qml") -PathType Leaf)) {
        throw "QML entry point is missing from the source tree."
    }
    New-Item -ItemType Directory -Path $qmlTarget -Force | Out-Null
    Copy-Item -Path (Join-Path $qmlSource "*") -Destination $qmlTarget -Recurse -Force
    # Keep the executable's runtime lookup deterministic even when a reused
    # Nuitka deployment predates a newly bundled face. The release always owns
    # the authoritative, licensed font set from the current source tree.
    $fontSource = Join-Path $projectRoot "src\ducai_studio\ui\assets\fonts"
    $fontTarget = Join-Path $releaseRoot "ui\assets\fonts"
    New-Item -ItemType Directory -Path $fontTarget -Force | Out-Null
    Copy-Item -Path (Join-Path $fontSource "*") -Destination $fontTarget -Force
    if (-not (Test-Path -LiteralPath (Join-Path $fontTarget "PlaywriteVN-Variable.ttf") -PathType Leaf)) {
        throw "Playwrite VN scene-text font is missing."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $fontTarget "Agbalumo-Regular.ttf") -PathType Leaf)) {
        throw "Agbalumo ink-calligraphy font is missing."
    }
    $additionalBrushFonts = @(
        "ComforterBrush-Regular.ttf",
        "WaterBrush-Regular.ttf",
        "Splash-Regular.ttf",
        "Hurricane-Regular.ttf"
    )
    foreach ($fontFile in $additionalBrushFonts) {
        if (-not (Test-Path -LiteralPath (Join-Path $fontTarget $fontFile) -PathType Leaf)) {
            throw "Bundled Vietnamese brush font is missing: $fontFile"
        }
    }
    $engineHostRoot = Join-Path $releaseRoot "engine_hosts"
    New-Item -ItemType Directory -Path $engineHostRoot -Force | Out-Null
    $sharedDataRoot = Join-Path $outputRootPath "DUCAI_STUDIO_DATA"
    $ocrRuntimePython = Join-Path $sharedDataRoot "paddle_runtime\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $ocrRuntimePython -PathType Leaf)) {
        throw "Paddle Python 3.11 is required to compile the public OCR host bytecode."
    }
    $compileHost = {
        param([string]$RuntimePython, [string]$SourceName)
        $source = Join-Path $projectRoot "src\ducai_studio\engine_hosts\$SourceName.py"
        $target = Join-Path $engineHostRoot "$SourceName.pyc"
        & $RuntimePython -c "import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], dfile='engine_hosts/'+sys.argv[3]+'.py', doraise=True)" $source $target $SourceName
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Could not compile engine host bytecode: $SourceName"
        }
    }
    & $compileHost $ocrRuntimePython "ocr_engine_host"
    & $compileHost $python "whisper_engine_host"
    & $compileHost $python "tts_engine_host"

    @"
DUCAI_STUDIO_DATA stays outside the GUI package so models and caches can update independently.
Set DUCAI_STUDIO_DATA_ROOT to move the shared data directory.
Run Diagnostics in the app before producing the first video.
"@ | Set-Content -LiteralPath (Join-Path $releaseRoot "DUCAI_STUDIO_DATA.README.txt") -Encoding UTF8

    & $python (Join-Path $PSScriptRoot "check-license-release.py") --release $releaseRoot
    if ($LASTEXITCODE -ne 0) { throw "Private licensing material found in customer build." }
    $files = Get-ChildItem $releaseRoot -Recurse -File | Sort-Object FullName
    $hashes = [ordered]@{}
    $releasePrefixLength = $releaseRoot.TrimEnd('\').Length + 1
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($releasePrefixLength).Replace('\', '/')
        $hashes[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{
        schema = "ducai-release-manifest-v1"
        product = "DUCAI STUDIO 2"
        version = $version
        platform = "windows-x86_64"
        gui_package = "DucaiStudio2.exe"
        external_data_root = "DUCAI_STUDIO_DATA"
        activation = "offline-device-license-v1"
        license_token_formats = @("DUCAI-L1", "DUCAI-L2", "DUCAI-L3")
        managed_engines = @(
            "PaddleOCR external Python runtime",
            "Whisper external runtime and optional large-v3 model",
            "Piper 1.6 runtime and ONNX voices",
            "VieNeu-TTS v3 Turbo 3.8.1 runtime and clone profiles",
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
