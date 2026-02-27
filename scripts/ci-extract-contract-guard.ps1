$ErrorActionPreference="Stop"
Push-Location (Resolve-Path "$PSScriptRoot\..")
try {
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

$f=".\main.py"
if (!(Test-Path $f)) { throw "Missing: $f" }
$lines = Get-Content $f

# Guard 1: Must unpack PDF extractor result
$bad = $lines | Select-String -Pattern 'text\s*=\s*_extract_text_from_pdf_stream\('
if ($bad) {
  $bad | ForEach-Object { "{0}:{1}" -f $_.LineNumber, $_.Line.Trim() } | Out-String | Write-Host
  throw "Regression: PDF extractor must be unpacked: text, pages = _extract_text_from_pdf_stream(...)."
}

$good = $lines | Select-String -Pattern 'text\s*,\s*pages\s*=\s*_extract_text_from_pdf_stream\('
if (-not $good) {
  throw "Regression: expected unpack 'text, pages = _extract_text_from_pdf_stream(...)' not found in main.py"
}

# Guard 2: For PDF responses, must pass pages=pages (not None)
# We'll confirm by locating ExtractResponseModel blocks that declare type=""pdf""
$pdfType = $lines | Select-String -Pattern 'type\s*=\s*"pdf"'
if ($pdfType.Count -lt 2) {
  throw "Regression: expected >=2 PDF ExtractResponseModel blocks (extract + extract-by-key). Found $($pdfType.Count)."
}

function Get-ReturnBlock([int]$nearLine) {
  # nearLine is 1-indexed
  $idx = $nearLine - 1

  # find the nearest 'return ExtractResponseModel(' above
  $start = $null
  for ($i=$idx; $i -ge [Math]::Max(0,$idx-30); $i--) {
    if ($lines[$i] -match 'return\s+ExtractResponseModel\s*\(') { $start = $i; break }
  }
  if ($start -eq $null) { return @() }

  # one-liner return
  if ($lines[$start] -match '\)') { return @($lines[$start]) }

  $blk = New-Object System.Collections.Generic.List[string]
  for ($j=$start; $j -lt [Math]::Min($lines.Length, $start+140); $j++) {
    $blk.Add($lines[$j])
    if ($lines[$j] -match '^\s*\)\s*,?\s*$') { break }
  }
  return $blk.ToArray()
}

$fail = @()
foreach ($h in $pdfType) {
  $blk = Get-ReturnBlock $h.LineNumber
  if ($blk.Count -eq 0) { $fail += "Could not locate ExtractResponseModel return block near line $($h.LineNumber)"; continue }

  $t = ($blk -join "`n")
  if ($t -match 'pages\s*=\s*None') { $fail += "PDF block near line $($h.LineNumber) uses pages=None"; continue }
  if ($t -notmatch 'pages\s*=\s*pages') { $fail += "PDF block near line $($h.LineNumber) missing pages=pages"; continue }
}

if ($fail.Count -gt 0) {
  Write-Host "`n--- FAILURES ---" -ForegroundColor Red
  $fail | ForEach-Object { Write-Host " - $_" }
  throw "Regression: extract response contract violated."
}

Write-Host "OK: ci-extract-contract-guard passed."
} finally { Pop-Location }

