Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

$allow = @(
  (Resolve-Path ".\providers\factory.py").Path,
  (Resolve-Path ".\core\providers.py").Path,
  (Resolve-Path ".\core\deps.py").Path
)

$pattern = '\bget_providers\s*\('

$hits = @()
foreach ($f in Get-CssPythonFiles ".") {
  $m = Select-String -Path $f -Pattern $pattern -AllMatches
  if ($m) {
    $full = (Resolve-Path $f).Path
    if ($allow -notcontains $full) {
      foreach ($h in $m) {
        $hits += ("{0}:{1}: forbidden get_providers() usage (provider drift). Use deps or app.state.providers." -f $h.Path, $h.LineNumber)
      }
    }
  }
}

if ($hits.Count -gt 0) {
  $hits | Sort-Object | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  throw "CI FAIL: get_providers() is only allowed in providers/factory.py, core/providers.py, core/deps.py"
}

Write-Host "OK: provider drift guard passed (no forbidden get_providers() calls)." -ForegroundColor Green