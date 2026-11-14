$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Create .venv with Python 3.11 and install .[dev] first." }
Push-Location $project
try {
$releaseRoot = Join-Path $project "release"
$buildRoot = Join-Path $releaseRoot ".build"
$bundle = Join-Path $releaseRoot "build\MiMonitoringService"
if (Test-Path -LiteralPath $bundle) {
    $resolvedBundle = (Resolve-Path -LiteralPath $bundle).Path
    $resolvedRelease = (Resolve-Path -LiteralPath $releaseRoot).Path
    if (-not $resolvedBundle.StartsWith($resolvedRelease + [IO.Path]::DirectorySeparatorChar)) {
        throw "Build output escaped the project release directory."
    }
    Remove-Item -LiteralPath $resolvedBundle -Recurse -Force
}
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $buildRoot "work"), (Join-Path $buildRoot "spec") -Force | Out-Null
& $python -m PyInstaller --noconfirm --clean --windowed --uac-admin --onefile --distpath $bundle --workpath (Join-Path $buildRoot "work") --specpath (Join-Path $buildRoot "spec") --name MiMonitoringService --icon (Join-Path $project "assets\mimonitoringservice.ico") --version-file (Join-Path $project "packaging\version_info.txt") --add-data "$project\docs\user-guide.html;docs" --add-data "$project\assets\mimonitoringservice-icon.png;assets" --paths (Join-Path $project "src") --collect-all tzdata (Join-Path $project "packaging\gui_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m PyInstaller --noconfirm --clean --noconsole --onefile --distpath $bundle --workpath (Join-Path $buildRoot "work") --specpath (Join-Path $buildRoot "spec") --name MiMonitoringService.Service --icon (Join-Path $project "assets\mimonitoringservice.ico") --version-file (Join-Path $project "packaging\version_info.txt") --paths (Join-Path $project "src") --hidden-import win32timezone --collect-all tzdata (Join-Path $project "packaging\service_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$guiHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bundle "MiMonitoringService.exe")).Hash
$serviceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bundle "MiMonitoringService.Service.exe")).Hash
$manifest = [ordered]@{
    product = "MiMonitoringService"
    version = "0.1.3"
    architecture = "x64"
    gui = "MiMonitoringService.exe"
    service = "MiMonitoringService.Service.exe"
    python_required = $false
    gui_sha256 = $guiHash
    service_sha256 = $serviceHash
    built_at_utc = [DateTime]::UtcNow.ToString("o")
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundle "build-manifest.json") -Encoding UTF8
Write-Host "Self-contained artifacts created in release\build\MiMonitoringService"
} finally {
    Pop-Location
}

