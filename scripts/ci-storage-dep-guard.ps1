param()

$ErrorActionPreference="Stop"

# Any router using storage.get_object/put_object/etc must have "storage" in the signature.
$routerFiles = Get-ChildItem -Recurse -Filter router.py |
  Where-Object { $_.FullName -notmatch "\\\.venv\\|\\__pycache__\\|\\out\\|\\\.git\\" }

$fail = $false

foreach ($rf in $routerFiles) {
  $p = $rf.FullName
  $lines = Get-Content $p

  for ($i=0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '\bstorage\.(get_object|put_object|delete_object|head_object)\b') {

      $def = $null
      for ($j=$i; $j -ge 0; $j--) {
        if ($lines[$j] -match '^(async\s+def|def)\s+') { $def = $lines[$j]; break }
      }

      if ($def -and $def -notmatch '\bstorage\b') {
        Write-Host ("STORAGE DEP DRIFT: {0}:{1} -> {2}" -f $p, ($i+1), $def.Trim()) -ForegroundColor Red
        $fail = $true
      }
    }
  }
}

if ($fail) { throw "CI FAIL: routes use storage.* without storage param (use StorageDep or pass storage explicitly)" }

Write-Host "CI STORAGE DEP OK" -ForegroundColor Green