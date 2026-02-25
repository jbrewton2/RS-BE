Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

Write-Host "[PREBUILD] scanning for UTF-8 BOM in .py files..." -ForegroundColor Cyan
$bad = @()

foreach ($f in Get-CssPythonFiles ".") {
  $bytes = [System.IO.File]::ReadAllBytes($f)
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    $bad += $f
  }
}

if ($bad.Count -gt 0) {
  $bad | Sort-Object | ForEach-Object { Write-Host ("BOM: " + $_) -ForegroundColor Red }
  throw "PREBUILD FAIL: UTF-8 BOM found in Python files. Remove BOMs."
}

Write-Host "[PREBUILD] python compileall..." -ForegroundColor Cyan
$dirs = @(
  ".\pricing",".\providers",".\questionnaire",".\rag",".\reviews",".\test"
)

foreach ($d in $dirs) {
  if (-not (Test-Path $d)) {
    throw "Missing directory: $d (are you in css-backend?)"
  }
}

python -m compileall -q @dirs
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

Write-Host "[PREBUILD] pytest -q..." -ForegroundColor Cyan
pytest -q

Write-Host "OK: prebuild validate passed." -ForegroundColor Green
