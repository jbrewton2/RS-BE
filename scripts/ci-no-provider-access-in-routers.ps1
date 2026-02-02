param()

$ErrorActionPreference="Stop"

# Only allow providers_from_request(...) in questionnaire\sessions_router.py (test seam + core router).
$allow = (Resolve-Path ".\questionnaire\sessions_router.py").Path

$routerFiles = Get-ChildItem -Recurse -Filter router.py |
  Where-Object {
    $_.FullName -notmatch "\\\.venv\\|\\__pycache__\\|\\out\\|\\\.git\\" -and
    (Resolve-Path $_.FullName).Path -ne $allow
  }

$fail = $false

foreach ($rf in $routerFiles) {
  $p = $rf.FullName
  $hits = Select-String -Path $p -Pattern 'providers_from_request\(' -AllMatches
  if ($hits) {
    $hits | ForEach-Object {
      Write-Host ("FORBIDDEN IN ROUTER: {0}:{1} {2}" -f $_.Path, $_.LineNumber, $_.Line.Trim()) -ForegroundColor Red
    }
    $fail = $true
  }
}

if ($fail) { throw "CI FAIL: routers must not call providers_from_request(); use StorageDep/ProvidersDep or pass storage explicitly." }

Write-Host "CI NO-PROVIDER-ACCESS-IN-ROUTERS OK" -ForegroundColor Green