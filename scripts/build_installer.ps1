$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$candidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $candidates) {
    throw "Inno Setup 6 compiler was not found. Install it on the build workstation, then rerun this script. Target servers do not need Inno Setup or Python."
}
$compiler = $candidates[0]
$gui = Join-Path $project "release\build\MiMonitoringService\MiMonitoringService.exe"
$service = Join-Path $project "release\build\MiMonitoringService\MiMonitoringService.Service.exe"
if (-not (Test-Path -LiteralPath $gui) -or -not (Test-Path -LiteralPath $service)) {
    throw "Build the self-contained executables with scripts\build.ps1 first."
}
Push-Location $project
try {
    & $compiler "packaging\installer.iss"
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

