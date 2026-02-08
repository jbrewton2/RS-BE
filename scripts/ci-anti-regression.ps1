$ErrorActionPreference="Stop"

Write-Host "=== CI Anti-Regression Guard (backend) ==="

# 1) Docs/ must not be tracked (shadow code risk). (Local untracked folders are allowed but ignored.)
$trackedDocs = (git ls-files | Select-String -Pattern '^Docs/' -SimpleMatch)
if ($trackedDocs) {
  throw "FAIL: Docs/ is tracked in git. Shadow code is not allowed. Use docs/ for documentation only."
}
# 2) scripts/scratch must not exist (use scripts/_scratch only)
if (Test-Path ".\scripts\scratch") {
  throw "FAIL: scripts/scratch exists. Use scripts/_scratch for non-authoritative experiments."
}

# 3) out/ must never be tracked
$trackedOut = (git ls-files | Select-String -Pattern '^out/' -SimpleMatch)
if ($trackedOut) {
  throw "FAIL: out/ contains tracked files. out/ must be generated-only and gitignored."
}

Write-Host "OK: anti-regression guard passed." -ForegroundColor Green

