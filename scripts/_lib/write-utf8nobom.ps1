param(
  [Parameter(Mandatory=$true)][string]$Path,

  # Accept either -Content (plain) or -ContentB64 (base64 UTF-8)
  [string]$Content = "",
  [string]$ContentB64 = ""
)

$repoRoot = (Resolve-Path ".").Path

if (-not [System.IO.Path]::IsPathRooted($Path)) {
  $Path = Join-Path $repoRoot $Path
}

$parent = Split-Path $Path -Parent
if ($parent -and -not (Test-Path $parent)) {
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

if ($ContentB64 -and $ContentB64.Trim().Length -gt 0) {
  $bytes = [Convert]::FromBase64String($ContentB64)
  $Content = [Text.Encoding]::UTF8.GetString($bytes)
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
