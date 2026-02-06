Set-StrictMode -Version Latest

function Get-CssPythonFiles {
  param([string]$Root = ".")
  $exclude = '\\\.venv\\|\\__pycache__\\|\\\.git\\|\\node_modules\\|\\out\\|\\dist\\|\\build\\|\\\.mypy_cache\\|\\\.pytest_cache\\'
  Get-ChildItem -Path $Root -Recurse -File -Filter *.py |
    Where-Object { $_.FullName -notmatch $exclude } |
    Select-Object -ExpandProperty FullName
}

function Get-CssRouterFiles {
  param([string]$Root = ".")
  $exclude = '\\\.venv\\|\\__pycache__\\|\\\.git\\|\\node_modules\\|\\out\\|\\dist\\|\\build\\|\\\.mypy_cache\\|\\\.pytest_cache\\'
  Get-ChildItem -Path $Root -Recurse -File |
    Where-Object {
      $_.FullName -notmatch $exclude -and
      ($_.Name -ieq "router.py" -or $_.Name -ilike "*_router.py")
    } |
    Select-Object -ExpandProperty FullName
}