$ErrorActionPreference="Stop"
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

$path = ".\scripts\green-gate.ps1"

$content = @'
# scripts/green-gate.ps1
# Green Gate (PS5.1-safe, parser-safe):
#  - Truth Gate (compile/tests/guards)
#  - Build+push ECR image from HEAD (or validate ImageTagOverride exists)
#  - Helm deploy to css-mock
#  - Enforce single-image pods
#  - Live /api/rag/analyze validation (no pipeline masking; curl exit codes enforced)
#
# NOTE: ReviewId/Token are NOT mandatory params to avoid interactive prompting.
#       Script fails fast with a clear message if missing.

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [string]$RepoPath,

  [Parameter(Mandatory=$true)]
  [string]$AwsProfile,

  [Parameter(Mandatory=$true)]
  [string]$AwsRegion,

  [Parameter(Mandatory=$true)]
  [string]$EcrRepo, # "css/css-backend"

  [Parameter(Mandatory=$true)]
  [string]$Namespace, # "css-mock"

  [Parameter(Mandatory=$true)]
  [string]$Deployment, # "css-backend"

  [Parameter(Mandatory=$true)]
  [string]$PodSelector, # "app=css-backend"

  [Parameter(Mandatory=$true)]
  [string]$BaseUrl, # "https://css-mock.shipcom.ai"

  [string]$Token,
  [string]$ReviewId,

  [string]$TokenEnvVar = "CSS_TOKEN",

  [ValidateSet("fast","balanced","deep")]
  [string]$ContextProfile = "balanced",

  [ValidateSet("risk_triage","strict_summary")]
  [string]$AnalysisIntent = "risk_triage",

  [int]$TopK = 3,

  [switch]$ForceReingest,

  [int]$MinEvidenceItems = 3,

  [switch]$AllowPromptTruncated,

  [switch]$LocalOnly,

  [string]$ImageTagOverride,

  [string]$OutDir
)

$ErrorActionPreference="Stop"

function Assert-Command([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Required command not found on PATH: $Name" }
}

function Write-Header([string]$Msg) {
  $bar = ("=" * 88)
  Write-Host ""
  Write-Host $bar
  Write-Host $Msg
  Write-Host $bar
}

function Ensure-Dir([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return }
  if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path | Out-Null }
}

function Read-Json([string]$Path) {
  $raw = Get-Content $Path -Raw
  return ($raw | ConvertFrom-Json)  # PS5.1 safe (no -Depth)
}

function To-JsonFile($Obj, [string]$Path) {
  $Obj | ConvertTo-Json -Depth 100 | Out-File -Encoding utf8 $Path
}

function Dump-K8sDiagnostics([string]$Ns,[string]$Dep,[string]$Selector,[string]$OutDir) {
  try {
    Ensure-Dir $OutDir
    $descPath = Join-Path $OutDir "k8s_describe_deploy.txt"
    $podsPath = Join-Path $OutDir "k8s_pods.txt"
    $logPath  = Join-Path $OutDir "k8s_backend_logs_tail.txt"

    kubectl -n $Ns describe "deploy/$Dep" | Out-File -Encoding utf8 $descPath
    kubectl -n $Ns get pods -l $Selector -o wide | Out-File -Encoding utf8 $podsPath

    $pod = (kubectl -n $Ns get pods -l $Selector --sort-by=.metadata.creationTimestamp -o jsonpath="{.items[-1:].metadata.name}")
    if ($pod) {
      kubectl -n $Ns logs $pod --tail=400 | Out-File -Encoding utf8 $logPath
    }
  } catch {
    Write-Host "Diagnostics dump failed (non-fatal): $($_.Exception.Message)"
  }
}

# -----------------------------
# Preconditions
# -----------------------------
Write-Header "GREEN GATE: Preconditions"

Assert-Command "git"
Assert-Command "python"

if (-not $LocalOnly) {
  Assert-Command "aws"
  Assert-Command "docker"
  Assert-Command "kubectl"
  Assert-Command "helm"
  Assert-Command "curl.exe"
}

if (-not (Test-Path $RepoPath)) { throw "RepoPath not found: $RepoPath" }

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $RepoPath "artifacts\green-gate"
}
Ensure-Dir $OutDir

# Token fallback from env var
if ([string]::IsNullOrWhiteSpace($Token)) {
  $Token = [Environment]::GetEnvironmentVariable($TokenEnvVar)
}

# Fail fast (no interactive prompts)
if ([string]::IsNullOrWhiteSpace($ReviewId)) {
  throw "ReviewId is required. Pass -ReviewId <guid>."
}
if ($ReviewId.Trim() -match '^\<.*review.*\>$') {
  throw "ReviewId looks like a placeholder ($ReviewId). Provide a real review GUID."
}
if (-not $LocalOnly -and [string]::IsNullOrWhiteSpace($Token)) {
  throw "Token is required for live API validation. Pass -Token <access_token> or set env:$TokenEnvVar."
}

# -----------------------------
# Repo sanity
# -----------------------------
Write-Header "GREEN GATE: Repo sanity"
cd $RepoPath

$st = (git status --porcelain)
if ($st -and $st.Trim().Length -gt 0) {
  throw "Working tree is not clean. Commit or stash before running Green Gate.`n$st"
}

$branch  = (git rev-parse --abbrev-ref HEAD).Trim()
$shaFull = (git rev-parse HEAD).Trim()
$sha7    = (git rev-parse --short=7 HEAD).Trim()

Write-Host "BRANCH = $branch"
Write-Host "HEAD   = $shaFull"
Write-Host "SHA7   = $sha7"
Write-Host "PODS   = selector '$PodSelector'"

# -----------------------------
# Local gates: Truth Gate
# -----------------------------
Write-Header "GREEN GATE: Local Truth Gate"

$truthGate = ".\scripts\truth-gate.ps1"
if (-not (Test-Path $truthGate)) { throw "Truth Gate script not found: $truthGate" }
& $truthGate
Write-Host "Local Truth Gate: PASS"

Write-Header "GREEN GATE: Extra RAG regression tests"
python -m pytest -q .\tests\test_rag_deterministic_signals_in_context.py
python -m pytest -q .\tests\test_rag_section_derived_risks.py
python -m pytest -q .\tests\test_rag_risk_materialization.py
Write-Host "Extra RAG regression tests: PASS"

if ($LocalOnly) {
  Write-Header "GREEN GATE: LocalOnly set -> stopping after local gates"
  Write-Host "Artifacts dir: $OutDir"
  exit 0
}

# -----------------------------
# Build + push image
# -----------------------------
Write-Header "GREEN GATE: Build + push image"

$env:AWS_PROFILE = $AwsProfile
$env:AWS_REGION  = $AwsRegion

$acct = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $acct) { throw "Failed to resolve AWS account id via sts get-caller-identity (profile=$AwsProfile region=$AwsRegion)" }

$registry = "$acct.dkr.ecr.$AwsRegion.amazonaws.com"

if ([string]::IsNullOrWhiteSpace($ImageTagOverride)) {
  $tag = "aws-$sha7"
} else {
  $tag = $ImageTagOverride.Trim()
}

$img = "$registry/$EcrRepo`:$tag"

Write-Host "AWS_PROFILE = $AwsProfile"
Write-Host "AWS_REGION  = $AwsRegion"
Write-Host "ACCOUNT     = $acct"
Write-Host "IMAGE       = $img"

aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin $registry | Out-Null

# If override tag is set, validate tag exists via describe-images (no JMESPath)
if (-not [string]::IsNullOrWhiteSpace($ImageTagOverride)) {
  $repoName = $EcrRepo
  $ok = $true
  try {
    aws ecr describe-images --region $AwsRegion --repository-name $repoName --image-ids imageTag=$tag --output json | Out-Null
  } catch {
    $ok = $false
  }
  if (-not $ok) {
    throw ("ImageTagOverride tag not found in ECR repo. repo=" + $repoName + " tag=" + $tag + " region=" + $AwsRegion)
  }
  Write-Host ("ECR tag exists: " + $repoName + ":" + $tag)
} else {
  $buildLog = Join-Path $OutDir "docker_build.log"
  $pushLog  = Join-Path $OutDir "docker_push.log"

  cmd.exe /c ("docker build -t ""$img"" . > ""$buildLog"" 2>&1")
  if ($LASTEXITCODE -ne 0) { throw "Docker build failed (exit=$LASTEXITCODE). See: $buildLog" }

  cmd.exe /c ("docker push ""$img"" > ""$pushLog"" 2>&1")
  if ($LASTEXITCODE -ne 0) { throw "Docker push failed (exit=$LASTEXITCODE). See: $pushLog" }
}

# -----------------------------
# Helm deploy
# -----------------------------
Write-Header "GREEN GATE: Helm deploy to css-mock"

$deployScript = ".\scripts\deploy-css-mock.ps1"
if (-not (Test-Path $deployScript)) { throw "Deploy script not found: $deployScript" }

$valuesPath = ".\deploy\helm\values-css-mock.yaml"
if (-not (Test-Path $valuesPath)) { throw "Expected helm values file missing: $valuesPath" }

try {
  & $deployScript -ImageTag $tag
}
finally {
  git restore $valuesPath | Out-Null
}

# -----------------------------
# Enforce single-image pods
# -----------------------------
Write-Header "GREEN GATE: Enforce single-image pods"

kubectl -n $Namespace rollout status "deploy/$Deployment"

# Use PS-safe jsonpath (no embedded single quotes)
$jsonpathPods = '{range .items[*]}{.metadata.name}{"|"}{.spec.containers[0].image}{"\n"}{end}'
$podMap = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath=$jsonpathPods
$podMapLines = ($podMap -split "`n" | Where-Object { $_.Trim().Length -gt 0 })

$bad = @()
foreach ($line in $podMapLines) {
  if ($line -notmatch [regex]::Escape(":$tag")) { $bad += $line }
}
if ($bad.Count -gt 0) {
  Write-Host "Found pods not on expected tag :$tag -> deleting"
  foreach ($b in $bad) {
    $name = ($b -split "\|")[0].Trim()
    Write-Host "deleting $name"
    try { kubectl -n $Namespace delete pod $name | Out-Null } catch { Write-Host "delete failed (ignored): $($_.Exception.Message)" }
  }
  kubectl -n $Namespace rollout status "deploy/$Deployment"
}

$podMap2 = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath=$jsonpathPods
Write-Host $podMap2

# -----------------------------
# Live /api/rag/analyze validation
# -----------------------------
Write-Header "GREEN GATE: Live /api/rag/analyze validation"

try {
  $payload = @{
    review_id       = $ReviewId
    mode            = "review_summary"
    analysis_intent = $AnalysisIntent
    context_profile = $ContextProfile
    top_k           = [int]$TopK
    force_reingest  = [bool]$ForceReingest.IsPresent
    debug           = $true
  }

  $payloadPath = Join-Path $OutDir "rag_payload.json"
  $respPath    = Join-Path $OutDir "rag_last.json"
  To-JsonFile $payload $payloadPath

  $url = "$BaseUrl/api/rag/analyze"
  Write-Host "POST $url"
  Write-Host "payload: $payloadPath"
  Write-Host "resp:    $respPath"

  # No pipeline masking: redirect stdout/stderr to file, check exit code
  $cmd = 'curl.exe -sS --fail-with-body -X POST "' + $url + '" ' +
         '-H "Authorization: Bearer ' + $Token + '" ' +
         '-H "Content-Type: application/json" ' +
         '--data-binary "@' + $payloadPath + '"'

  cmd.exe /c ($cmd + ' > "' + $respPath + '" 2>&1')
  if ($LASTEXITCODE -ne 0) {
    $preview = ""
    try { $preview = (Get-Content $respPath -TotalCount 80 | Out-String) } catch { $preview = "<unable to read response file>" }
    throw ("GREEN GATE FAIL: curl returned non-zero exit code: " + $LASTEXITCODE + "`n" + $preview)
  }

  $resp = Read-Json $respPath

  # warnings
  $warnings = @()
  if ($resp.warnings -is [System.Array]) { $warnings = @($resp.warnings) }
  elseif ($resp.warnings) { $warnings = @("$($resp.warnings)") }
  Write-Host ("warnings = " + ($warnings -join ", "))

  if ($warnings -contains "ingest_failed") {
    $ingStr = ""
    if ($resp.stats -and $resp.stats.ingest) { $ingStr = ($resp.stats.ingest | ConvertTo-Json -Depth 20) }
    throw "GREEN GATE FAIL: warnings includes ingest_failed. ingest stats: $ingStr"
  }

  if (-not $AllowPromptTruncated -and ($warnings -contains "prompt_truncated")) {
    $dp = $resp.stats.debug_prompt_len
    $cu = $resp.stats.context_used_chars
    $cm = $resp.stats.context_max_chars
    $tk = $resp.stats.top_k_effective
    $rt = $resp.stats.retrieved_total
    throw ("GREEN GATE FAIL: warnings includes prompt_truncated. " +
           "debug_prompt_len=" + $dp + "; context_used_chars=" + $cu + "/" + $cm +
           "; top_k_effective=" + $tk + "; retrieved_total=" + $rt +
           ". (Pass -AllowPromptTruncated to allow temporarily.)")
  }

  # evidence counts
  $sections = @()
  if ($resp.sections -is [System.Array]) { $sections = @($resp.sections) }

  $totalEvidence = 0
  foreach ($s in $sections) {
    $ev = $s.evidence
    if ($ev -is [System.Array]) { $totalEvidence += $ev.Count }
  }

  $retrievedCountsTotal = 0
  if ($resp.retrieved_counts) {
    foreach ($pp in $resp.retrieved_counts.PSObject.Properties) {
      $retrievedCountsTotal += [int]$pp.Value
    }
  }

  Write-Host "sections = $($sections.Count)"
  Write-Host "retrieved_counts_total = $retrievedCountsTotal"
  Write-Host "totalEvidenceItems = $totalEvidence (min required = $MinEvidenceItems)"

  if ($totalEvidence -lt $MinEvidenceItems) {
    throw "GREEN GATE FAIL: evidence gate failed (totalEvidenceItems=$totalEvidence, retrieved_counts_total=$retrievedCountsTotal)."
  }

  Write-Header "GREEN GATE: PASS"
  Write-Host "Repo       : $RepoPath"
  Write-Host "Branch     : $branch"
  Write-Host "SHA7       : $sha7"
  Write-Host "ImageTag   : $tag"
  Write-Host "Image      : $img"
  Write-Host "Namespace  : $Namespace"
  Write-Host "Deployment : $Deployment"
  Write-Host "Pods       : $PodSelector"
  Write-Host "Artifacts  : $OutDir"
}
catch {
  Dump-K8sDiagnostics -Ns $Namespace -Dep $Deployment -Selector $PodSelector -OutDir $OutDir
  throw
}
'@

# Write UTF-8 no BOM
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $content, $utf8NoBom)

# Parse check
powershell.exe -NoProfile -Command "Set-StrictMode -Off; . '$((Resolve-Path $path).Path)' -RepoPath 'x' -AwsProfile 'x' -AwsRegion 'x' -EcrRepo 'x' -Namespace 'x' -Deployment 'x' -PodSelector 'x' -BaseUrl 'x' -ReviewId 'x' -LocalOnly" | Out-Null
Write-Host "OK: green-gate.ps1 rewritten + parses"