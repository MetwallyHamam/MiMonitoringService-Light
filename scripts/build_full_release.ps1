$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$releaseRoot = Join-Path $project "release"
$packageName = "MiMonitoringService-v0.1.1-win-x64-FULL-PRIVATE"
$packageStageRoot = Join-Path $releaseRoot ".build\package"
$package = Join-Path $packageStageRoot $packageName
$archive = Join-Path $releaseRoot "$packageName.zip"

& (Join-Path $PSScriptRoot "build.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "build_license_generator.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path -LiteralPath $package) {
    $resolvedPackage = (Resolve-Path -LiteralPath $package).Path
    $resolvedStageRoot = (Resolve-Path -LiteralPath $packageStageRoot).Path
    if (-not $resolvedPackage.StartsWith($resolvedStageRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "Package path escaped the release staging directory."
    }
    Remove-Item -LiteralPath $resolvedPackage -Recurse -Force
}
if (Test-Path -LiteralPath $archive) {
    Remove-Item -LiteralPath $archive -Force
}

$customer = Join-Path $package "Customer"
$support = Join-Path $package "MillensysSupport-PRIVATE"
New-Item -ItemType Directory -Path $customer, $support -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $releaseRoot "build\MiMonitoringService\MiMonitoringService.exe") -Destination $customer
Copy-Item -LiteralPath (Join-Path $releaseRoot "build\MiMonitoringService\MiMonitoringService.Service.exe") -Destination $customer
Copy-Item -LiteralPath (Join-Path $releaseRoot "build\MiMonitoringService\build-manifest.json") -Destination $customer
Copy-Item -LiteralPath (Join-Path $project "README.md") -Destination $customer
Copy-Item -LiteralPath (Join-Path $project "DEPLOYMENT.md") -Destination $customer
Copy-Item -LiteralPath (Join-Path $project "docs\user-guide.html") -Destination (Join-Path $customer "MiMonitoringService-User-Guide.html")
Copy-Item -LiteralPath (Join-Path $releaseRoot "build\MillensysLicenseGenerator\MiMonitoringService.LicenseGenerator.exe") -Destination $support
Copy-Item -LiteralPath (Join-Path $releaseRoot "build\MillensysLicenseGenerator\support-private") -Destination $support -Recurse

$hashes = Get-ChildItem -LiteralPath $package -Recurse -File | ForEach-Object {
    [ordered]@{
        file = [IO.Path]::GetRelativePath($package, $_.FullName)
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
    }
}
$hashes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $package "SHA256SUMS.json") -Encoding UTF8
Compress-Archive -LiteralPath $package -DestinationPath $archive -CompressionLevel Optimal
Write-Host "Full private release package created at release\$packageName.zip"
