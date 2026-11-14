$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$privateSource = Join-Path $project "support-private"
$releaseRoot = Join-Path $project "release"
$buildRoot = Join-Path $releaseRoot ".build"
$output = Join-Path $releaseRoot "build\MillensysLicenseGenerator"
if (-not (Test-Path -LiteralPath $python)) { throw "Development virtual environment is missing." }
if (-not (Test-Path -LiteralPath (Join-Path $privateSource "license-signing-private.pem"))) {
    throw "Run scripts\init_license_keys.py before building the generator."
}
if (-not (Test-Path -LiteralPath (Join-Path $privateSource "smtp-app-password.txt"))) {
    throw "Create support-private\smtp-app-password.txt before building the generator."
}
if (Test-Path -LiteralPath $output) {
    $resolvedOutput = (Resolve-Path -LiteralPath $output).Path
    $resolvedRoot = (Resolve-Path -LiteralPath $releaseRoot).Path
    if (-not $resolvedOutput.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "Support build path escaped its output directory."
    }
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $output "support-private") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $buildRoot "work"), (Join-Path $buildRoot "spec") -Force | Out-Null
Push-Location $project
try {
    & $python -m PyInstaller --noconfirm --clean --windowed --onefile `
        --distpath $output --workpath (Join-Path $buildRoot "work") `
        --specpath (Join-Path $buildRoot "spec") `
        --name MiMonitoringService.LicenseGenerator `
        --icon (Join-Path $project "assets\mimonitoringservice.ico") `
        --paths (Join-Path $project "src") --paths $project `
        --add-data "$project\assets\mimonitoringservice-icon.png;assets" `
        (Join-Path $project "packaging\license_generator_entry.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $env:PYTHONPATH = Join-Path $project "src"
    & $python scripts\provision_support_bundle.py $output
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Private Millensys support tool created in release\build\MillensysLicenseGenerator"
} finally {
    Pop-Location
}
