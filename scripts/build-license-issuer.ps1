param([string]$OutputRoot = "")
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\python.exe")).Path
if (-not $OutputRoot) { $OutputRoot = Join-Path $projectRoot "..\..\outputs" }
$root = [IO.Path]::GetFullPath($OutputRoot)
$version = (& $python -c "from ducai_studio import __version__; print(__version__)" ).Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read the owner tool version." }
$ownerRoot = Join-Path $root "DUCAI-LICENSE-OWNER-PRIVATE-$version"
if (Test-Path -LiteralPath $ownerRoot) {
    throw "Owner output already exists; do not overwrite issuer or private records."
}
Push-Location $projectRoot
try {
    & $python -m nuitka --standalone --enable-plugin=tk-inter --windows-console-mode=disable `
        --assume-yes-for-downloads --output-dir="$ownerRoot" `
        --output-filename=DUCAI-Key-Issuer.exe --include-package=cryptography `
        (Join-Path $PSScriptRoot "license-issuer.py")
    if ($LASTEXITCODE -ne 0) { throw "Owner issuer build failed." }
    $dist = Join-Path $ownerRoot "license-issuer.dist"
    if (-not (Test-Path -LiteralPath (Join-Path $dist "DUCAI-Key-Issuer.exe"))) {
        throw "Owner executable is missing."
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\offline-licensing-owner.md") `
        -Destination (Join-Path $dist "HUONG-DAN-CHU-TOOL.md")
    Write-Output $dist
} finally { Pop-Location }
