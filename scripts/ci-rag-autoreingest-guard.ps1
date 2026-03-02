$ErrorActionPreference="Stop"
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

$f = ".\rag\service.py"
if (!(Test-Path $f)) { throw "Missing: $f" }

# Hard guard: must not be a PowerShell file
$head = Get-Content $f -TotalCount 5
if (($head -join "`n") -match 'Set-StrictMode|PowerShell') {
  throw "Regression: rag/router.py looks like PowerShell (file corruption)."
}

$need = @(
  "retrieval_or_context_empty",
  "auto_reingest_used"
)

foreach ($s in $need) {
  $hit = rg -n -F -e $s $f
  if (-not $hit) {
    throw "Regression: expected marker not found in $f : $s"
  }
}

Write-Host "OK: ci-rag-autoreingest-guard passed."
