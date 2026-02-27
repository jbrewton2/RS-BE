$ErrorActionPreference="Stop"
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

$f = ".\main.py"
if (!(Test-Path $f)) { throw "Missing: $f" }

$lines = Get-Content $f

# -------------------------
# Guard 1: PDF extractor MUST be unpacked as (text, pages)
# -------------------------
$badAssign = $lines | Select-String -Pattern 'text\s*=\s*_extract_text_from_pdf_stream\('
if ($badAssign) {
  $badAssign | ForEach-Object { "{0}:{1}" -f $_.LineNumber, $_.Line.Trim() } | Out-String | Write-Host
  throw "Regression: PDF extractor must be unpacked: text, pages = _extract_text_from_pdf_stream(...). Found bad assignment."
}

$goodAssign = $lines | Select-String -Pattern 'text\s*,\s*pages\s*=\s*_extract_text_from_pdf_stream\('
if (-not $goodAssign) {
  throw "Regression: expected 'text, pages = _extract_text_from_pdf_stream(...)' not found in main.py"
}

# -------------------------
# Guard 2: PDF ExtractResponseModel blocks must include pages=pages and must NOT include pages=None
# Handles:
#   A) one-liner: return ExtractResponseModel(... type="pdf" ... pages=pages ...)
#   B) multiline: return ExtractResponseModel( ... type="pdf" ... pages=pages ... )
# -------------------------
function Get-ExtractBlock {
  param(
    [string[]]$L,
    [int]$StartLineIndex
  )

  # Find the nearest "return ExtractResponseModel(" at or above StartLineIndex (scan up a bit)
  $start = $null
  for ($i = $StartLineIndex; $i -ge [Math]::Max(0, $StartLineIndex - 25); $i--) {
    if ($L[$i] -match 'return\s+ExtractResponseModel\s*\(') { $start = $i; break }
  }
  if ($start -eq $null) { return @() }

  # If it's a one-liner (has ')' on same line), return just that line
  if ($L[$start] -match '\)') { return @($L[$start]) }

  # Otherwise, collect until we hit a line containing ')' that closes the call.
  $block = New-Object System.Collections.Generic.List[string]
  for ($j = $start; $j -lt [Math]::Min($L.Length, $start + 120); $j++) {
    $block.Add($L[$j])
    if ($L[$j] -match '^\s*\)\s*$' -or $L[$j] -match '^\s*\)\s*,?\s*$') { break }
  }
  return $block.ToArray()
}

# Find all occurrences of type="pdf" inside ExtractResponseModel blocks
$pdfTypeHits = @()
for ($i = 0; $i -lt $lines.Length; $i++) {
  if ($lines[$i] -match 'type\s*=\s*"pdf"') {
    $blk = Get-ExtractBlock -L $lines -StartLineIndex $i
    if ($blk.Count -gt 0) {
      $pdfTypeHits += [pscustomobject]@{ LineIndex = $i; Block = $blk }
    }
  }
}

if ($pdfTypeHits.Count -lt 2) {
  throw "Regression: expected at least 2 PDF ExtractResponseModel blocks (extract + extract-by-key). Found $($pdfTypeHits.Count)."
}

$failBlocks = @()
foreach ($h in $pdfTypeHits) {
  $blkText = ($h.Block -join "`n")

  if ($blkText -match 'pages\s*=\s*None') {
    $failBlocks += "PDF block near line $($h.LineIndex+1) uses pages=None (not allowed)."
    continue
  }

  if ($blkText -notmatch 'pages\s*=\s*pages') {
    $failBlocks += "PDF block near line $($h.LineIndex+1) is missing pages=pages."
    continue
  }
}

if ($failBlocks.Count -gt 0) {
  Write-Host "`n--- DEBUG: failing PDF ExtractResponseModel blocks ---"
  $failBlocks | ForEach-Object { Write-Host $_ }

  Write-Host "`n--- DEBUG: show PDF ExtractResponseModel return blocks (first 2) ---"
  $shown = 0
  foreach ($h in $pdfTypeHits) {
    if ($shown -ge 2) { break }
    Write-Host "----- PDF BLOCK (near line $($h.LineIndex+1)) -----"
    ($h.Block -join "`n") | Write-Host
    $shown++
  }

  throw "Regression: PDF ExtractResponseModel contract violated (missing pages=pages and/or pages=None present)."
}

Write-Host "OK: ci-extract-contract-guard passed."
