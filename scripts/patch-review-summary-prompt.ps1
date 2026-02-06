$ErrorActionPreference="Stop"

$path = ".\rag\service.py"
if (-not (Test-Path $path)) { throw "Missing: $path" }

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item $path "$path.bak_dedupe_prompt_$ts" -Force

$raw = Get-Content $path -Raw

# Locate ONLY the review_summary prompt f-string inside rag/service.py
$pattern = '(?s)(if m == "review_summary":\s*\r?\n\s*prompt = f""")(?<body>.*?)(\r?\n\s*"""\.strip\(\)\s*\r?\n\s*else:)'
$match = [regex]::Match($raw, $pattern)
if (-not $match.Success) { throw "Could not locate review_summary prompt f-string block." }

$body = $match.Groups["body"].Value

# 1) Normalize any unicode dashes between digits (e.g., 2–4 or 2—4) to "2-4"
#    (Avoids corrupted characters entirely.)
$body = [regex]::Replace($body, '(?<=\d)[\u2013\u2014](?=\d)', '-')

# 2) Remove ALL occurrences of the ARCHITECTURE / TECH APPROACH line (review_summary only)
$body = [regex]::Replace($body, '(?m)^\s*ARCHITECTURE\s*/\s*TECH\s*APPROACH\s*\r?\n', '')

# 3) Collapse duplicated "second copy" of the big block:
#    If the prompt repeats a second "OVERVIEW ..." after "RECOMMENDED INTERNAL ACTIONS",
#    delete that second chunk up to RETRIEVED CONTEXT.
$body = [regex]::Replace(
  $body,
  '(?s)\r?\nRECOMMENDED INTERNAL ACTIONS\r?\n\r?\nOVERVIEW\r?\n.*?(?=\r?\nRETRIEVED CONTEXT\r?\n)',
  "`r`nRECOMMENDED INTERNAL ACTIONS`r`n",
  1
)

# 4) Replace the ENTIRE SECTIONS list with the single authoritative one
$sectionsPattern = '(?s)SECTIONS \(exact order\)\s*\r?\n.*?(?=\r?\nRETRIEVED CONTEXT\r?\n)'
if ($body -notmatch $sectionsPattern) { throw "Could not find SECTIONS block inside review_summary prompt to replace." }

$sectionsReplacement = @"
SECTIONS (exact order)

OVERVIEW

MISSION & OBJECTIVE
SCOPE OF WORK

SECURITY, COMPLIANCE & HOSTING CONSTRAINTS
- paragraph allowed (2-4 sentences) if needed for clarity, then bullets.

DATA / INTEGRATIONS
DELIVERABLES / TIMELINES
LEGAL / DATA RIGHTS
FINANCIAL
SUBMISSION / EVALUATION

STAKEHOLDERS & OWNERSHIP
RISK REGISTER (TOP 8)
GAPS / QUESTIONS FOR GOVERNMENT
RECOMMENDED INTERNAL ACTIONS

RETRIEVED CONTEXT
"@.TrimEnd()

$body = [regex]::Replace($body, $sectionsPattern, $sectionsReplacement, 1)

# Reassemble and write UTF-8 no BOM
$updated = $raw.Substring(0, $match.Groups["body"].Index) + $body + $raw.Substring($match.Groups["body"].Index + $match.Groups["body"].Length)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Resolve-Path $path), $updated, $utf8NoBom)

Write-Host "OK: review_summary prompt deduped + ARCHITECTURE removed + SECTIONS normalized" -ForegroundColor Green

python -m compileall .\rag\service.py
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }

Write-Host "OK: tests pass" -ForegroundColor Green

Write-Host "== PROOF (first matches) ==" -ForegroundColor Cyan
rg -n "SECTIONS \(exact order\)" .\rag\service.py | Select-Object -First 5
rg -n "ARCHITECTURE / TECH APPROACH" .\rag\service.py | Select-Object -First 5
rg -n "STAKEHOLDERS & OWNERSHIP" .\rag\service.py | Select-Object -First 5
rg -n "RISK REGISTER \(TOP 8\)" .\rag\service.py | Select-Object -First 5