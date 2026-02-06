Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

$fail = @()

foreach ($f in Get-CssPythonFiles ".") {
  # force array always
  $lines = @(Get-Content -LiteralPath $f)

  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^\s*(async\s+def|def)\s+') {
      $hdr = $lines[$i]
      $j = $i + 1
      while ($j -lt $lines.Count -and $hdr -notmatch '\)\s*(:|->)') {
        $hdr += " " + ($lines[$j].Trim())
        $j++
      }

      $matches = [regex]::Matches($hdr, '\brequest\s*:\s*Request\b')
      if ($matches.Count -gt 1) {
        $fail += ("{0}:{1}: duplicate 'request: Request' in function signature" -f $f, ($i+1))
      }
    }
  }
}

if ($fail.Count -gt 0) {
  $fail | Sort-Object | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  throw "CI FAIL: python sanity checks failed."
}

Write-Host "OK: python sanity checks passed." -ForegroundColor Green