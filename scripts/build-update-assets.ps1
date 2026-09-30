param(
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$PrivateKeyPath,
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Resolve-Path (Join-Path $projectRoot ".venv\Scripts\python.exe")).Path
$resolvedRelease = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$resolvedKey = (Resolve-Path -LiteralPath $PrivateKeyPath).Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path (Split-Path $resolvedRelease -Parent) ((Split-Path $resolvedRelease -Leaf) + "-UPDATE")
}
$outputPath = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $outputPath) {
    $leaf = Split-Path $outputPath -Leaf
    if (-not $leaf.EndsWith("-UPDATE")) { throw "Refusing to replace a non-update output directory." }
    Remove-Item -LiteralPath $outputPath -Recurse -Force
}
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

Push-Location $projectRoot
try {
    & $python scripts/build-update-assets.py --release-root $resolvedRelease --output-root $outputPath --private-key $resolvedKey
    if ($LASTEXITCODE -ne 0) { throw "Update asset build failed." }
    $forbidden = Get-ChildItem $outputPath -Recurse -File | Where-Object {
        $_.Extension -in @(".py", ".whl", ".pem", ".key")
    }
    if ($forbidden) { throw "Public update output contains forbidden source or key files." }
    Write-Output $outputPath
}
finally {
    Pop-Location
}
