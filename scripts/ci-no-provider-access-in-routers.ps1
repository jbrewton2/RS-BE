Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

$allow = @(
  (Resolve-Path ".\core\deps.py").Path,
  (Resolve-Path ".\core\providers.py").Path,
  (Resolve-Path ".\questionnaire\sessions_router.py").Path
)

$hits = @()
foreach ($f in Get-CssRouterFiles ".") {
  $m = Select-String -Path $f -Pattern 'providers_from_request\s*\(' -AllMatches
  if ($m) {
    $full = (Resolve-Path $f).Path
    if ($allow -notcontains $full) {
      foreach ($h in $m) {
        $hits += ("{0}:{1}: providers_from_request(...) is forbidden in routers (use deps / pass providers)" -f $h.Path, $h.LineNumber)
      }
    }
  }
}

if ($hits.Count -gt 0) {
  $hits | Sort-Object | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  throw "CI FAIL: routers must not call providers_from_request(); only core deps/providers and questionnaire\sessions_router.py may use it."
}

Write-Host "OK: no forbidden providers_from_request() usage in routers." -ForegroundColor Green