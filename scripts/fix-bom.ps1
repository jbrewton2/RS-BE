Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

. "$PSScriptRoot\_lib\scan-paths.ps1"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$fixed = 0
foreach ($f in Get-CssPythonFiles ".") {
  $bytes = [System.IO.File]::ReadAllBytes($f)
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    # strip BOM
    $newBytes = $bytes[3..($bytes.Length-1)]
    $text = [Text.Encoding]::UTF8.GetString($newBytes)
    [System.IO.File]::WriteAllText($f, $text, $utf8NoBom)
    Write-Host ("FIXED BOM: " + $f) -ForegroundColor Yellow
    $fixed++
  }
}

Write-Host ("OK: removed BOM from {0} file(s)" -f $fixed) -ForegroundColor Green