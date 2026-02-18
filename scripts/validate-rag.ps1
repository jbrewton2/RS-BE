param()

$ErrorActionPreference="Stop"

# Run from repo root
if (-not (Test-Path ".\rag\service.py")) { throw "Run from css-backend repo root (rag/service.py not found). Current: $((Get-Location).Path)" }

Write-Host "[VALIDATE] py_compile key RAG modules"
python -m py_compile ".\rag\contracts.py"
python -m py_compile ".\rag\router.py"
python -m py_compile ".\rag\service.py"

Write-Host "[VALIDATE] compileall rag/"
python -m compileall ".\rag" | Out-Host

Write-Host "[VALIDATE] quick invariants"
$txt = Get-Content ".\rag\service.py" -Raw
foreach ($needle in @("source_confidence_tier", "source_type", "_materialize_risks_from_heuristic_hits", "_materialize_risks_from_inference")) {
  if ($txt.IndexOf($needle) -lt 0) { throw "Missing expected marker in rag/service.py: $needle" }
}

Write-Host "[VALIDATE] PASS"
exit 0
