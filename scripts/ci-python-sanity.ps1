param()

$ErrorActionPreference="Stop"

# Fail on duplicate "request: Request" in function defs
$hits = Select-String -Path ".\**\*.py" -Pattern 'def\s+\w+\(.*request:\s*Request.*request:\s*Request' -AllMatches |
  Where-Object { $_.Path -notmatch "\\\.venv\\|\\__pycache__\\|\\out\\|\\\.git\\" }

if ($hits) {
  $hits | ForEach-Object { "{0}:{1} {2}" -f $_.Path, $_.LineNumber, $_.Line.Trim() }
  throw "CI FAIL: duplicate request: Request in function signature"
}

Write-Host "CI PYTHON SANITY OK" -ForegroundColor Green