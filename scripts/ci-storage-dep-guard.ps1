Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

$allowedException = (Resolve-Path ".\questionnaire\sessions_router.py").Path
$methodPattern = 'storage\.(get_object|put_object|delete_object|head_object)\s*\('
$defPattern = '^\s*(async\s+def|def)\s+([A-Za-z0-9_]+)\s*\('

function Get-DefHeader {
  param([string[]]$Lines, [int]$Index)

  for ($i = $Index; $i -ge 0; $i--) {
    if ($Lines[$i] -match $defPattern) {
      $hdr = $Lines[$i]
      $j = $i + 1
      while ($j -lt $Lines.Length -and $hdr -notmatch '\)\s*(:|->)') {
        $hdr += "`n" + $Lines[$j]
        $j++
      }
      return @{ Start = $i; Header = $hdr }
    }
  }
  return $null
}

$fail = @()

foreach ($f in Get-CssRouterFiles ".") {
  $full = (Resolve-Path $f).Path
  if ($full -eq $allowedException) { continue }

  $lines = Get-Content -LiteralPath $f
  for ($i = 0; $i -lt $lines.Length; $i++) {
    if ($lines[$i] -match $methodPattern) {
      $def = Get-DefHeader -Lines $lines -Index $i
      if (-not $def) {
        $fail += ("{0}:{1}: storage.* used but no enclosing def found above" -f $f, ($i+1))
        continue
      }
      if ($def.Header -notmatch '\bstorage\b') {
        $fail += ("{0}:{1}: storage.* used but route signature missing 'storage' dependency. Function header starts at line {2}." -f $f, ($i+1), ($def.Start+1))
      }
    }
  }
}

if ($fail.Count -gt 0) {
  $fail | Sort-Object | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  throw "CI FAIL: StorageDep guard failed. Routers using storage.* must accept storage in signature (except questionnaire\sessions_router.py)."
}

Write-Host "OK: storage dependency guard passed." -ForegroundColor Green