$ErrorActionPreference="Stop"
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

$f = ".\providers\impl\vector_opensearch.py"
if (!(Test-Path $f)) { throw "Missing: $f" }

$need = @(
  "auth expired during search; refreshing client and retrying once",
  "auth expired during bulk; refreshing client and retrying once",
  "auth expired during indices.exists; refreshing client and retrying once",
  "auth expired during indices.create; refreshing client and retrying once",
  "def _refresh_client",
  "def _build_client"
)

foreach ($s in $need) {
  $hit = rg -n -F -e $s $f
  if (-not $hit) {
    throw "Regression: expected marker not found in $f : $s"
  }
}

Write-Host "OK: ci-opensearch-refresh-guard passed."
