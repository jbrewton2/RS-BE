Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

function Invoke-Step {
  param([string]$Label, [string]$File)
  Write-Host ("--- " + $Label + " ---") -ForegroundColor Cyan
  pwsh -NoProfile -ExecutionPolicy Bypass -File $File
  if ($LASTEXITCODE -ne 0) {
    throw ("Truth gate failed at: " + $Label)
  }
}

Write-Host "=== CSS BACKEND TRUTH GATE ===" -ForegroundColor Cyan

Invoke-Step "prebuild-validate" "$PSScriptRoot\prebuild-validate.ps1"
Invoke-Step "ci-python-sanity" "$PSScriptRoot\ci-python-sanity.ps1"
Invoke-Step "ci-provider-drift-guard" "$PSScriptRoot\ci-provider-drift-guard.ps1"
Invoke-Step "ci-no-provider-access-in-routers" "$PSScriptRoot\ci-no-provider-access-in-routers.ps1"
Invoke-Step "ci-storage-dep-guard" "$PSScriptRoot\ci-storage-dep-guard.ps1"

Write-Host "--- ci-anti-regression ---" -ForegroundColor Cyan
& "$PSScriptRoot\ci-anti-regression.ps1"

Write-Host "OK: truth gate passed." -ForegroundColor Green
