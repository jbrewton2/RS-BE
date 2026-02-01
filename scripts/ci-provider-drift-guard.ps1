param()

$ErrorActionPreference = "Stop"

$allowed = @(
  (Join-Path $PSScriptRoot "..\providers\factory.py"),
  (Join-Path $PSScriptRoot "..\core\providers.py")
) | ForEach-Object { (Resolve-Path $_).Path }

$hits = Select-String -Path (Join-Path $PSScriptRoot "..\**\*.py") -Pattern "get_providers\(|_providers\(" -AllMatches |
  Where-Object {
    $_.Path -notmatch "\\test\\" -and
    ($allowed -notcontains (Resolve-Path $_.Path).Path)
  }

if ($hits) {
  $hits | ForEach-Object { "{0}:{1} {2}" -f $_.Path, $_.LineNumber, $_.Line.Trim() }
  throw "CI PROVIDER DRIFT FAIL: forbidden provider patterns exist outside allowlist."
}

Write-Host "CI PROVIDER DRIFT OK" -ForegroundColor Green