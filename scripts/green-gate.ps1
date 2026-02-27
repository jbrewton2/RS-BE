# scripts/green-gate.ps1
# Green Gate (PS5.1-safe, parser-safe):
#  - Truth Gate (compile/tests/guards)
#  - Build+push ECR image from HEAD (or validate ImageTagOverride exists)
#  - Helm deploy to css-mock
#  - Enforce single-image pods
#  - Live /api/rag/analyze validation (curl exit codes enforced; no pipeline masking)

[CmdletBinding()]
param(
  [string]$RepoPath,
  [Parameter(Mandatory=$true)][string]$AwsProfile,
  [Parameter(Mandatory=$true)][string]$AwsRegion,
  [Parameter(Mandatory=$true)][string]$EcrRepo,
  [Parameter(Mandatory=$true)][string]$Namespace,
  [Parameter(Mandatory=$true)][string]$Deployment,
  [Parameter(Mandatory=$true)][string]$PodSelector,
  [Parameter(Mandatory=$true)][string]$BaseUrl,

  [string]$Token,
  [Parameter(Mandatory=$true)][string]$ReviewId,
  [string]$TokenEnvVar = "CSS_TOKEN",

  [ValidateSet("fast","balanced","deep")][string]$ContextProfile = "balanced",
  [ValidateSet("risk_triage","strict_summary")][string]$AnalysisIntent = "risk_triage",
  [int]$TopK = 3,

  [switch]$ForceReingest,
  [int]$MinEvidenceItems = 3,
  [switch]$AllowPromptTruncated,
  [switch]$LocalOnly,

  [string]$ImageTagOverride,
  [string]$OutDir
)

$ErrorActionPreference="Stop"
if ([string]::IsNullOrWhiteSpace($RepoPath)) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
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
  return ($raw | ConvertFrom-Json)
}

function To-JsonFile($Obj, [string]$Path) {
  $Obj | ConvertTo-Json -Depth 100 | Out-File -Encoding utf8 $Path
}

function Dump-K8sDiagnostics([string]$Ns,[string]$Dep,[string]$Selector,[string]$OutDir) {
  try {
    Ensure-Dir $OutDir
    kubectl -n $Ns describe "deploy/$Dep" | Out-File -Encoding utf8 (Join-Path $OutDir "k8s_describe_deploy.txt")
    kubectl -n $Ns get pods -l $Selector -o wide | Out-File -Encoding utf8 (Join-Path $OutDir "k8s_pods.txt")
    $pod = (kubectl -n $Ns get pods -l $Selector --sort-by=.metadata.creationTimestamp -o jsonpath="{.items[-1:].metadata.name}")
    if ($pod) { kubectl -n $Ns logs $pod --tail=400 | Out-File -Encoding utf8 (Join-Path $OutDir "k8s_backend_logs_tail.txt") }
  } catch { Write-Host "Diagnostics dump failed (non-fatal): $($_.Exception.Message)" }
}

Write-Header "GREEN GATE: Preconditions"
Assert-Command "git"
Assert-Command "python"
if (-not $LocalOnly) { Assert-Command "aws"; Assert-Command "docker"; Assert-Command "kubectl"; Assert-Command "helm"; Assert-Command "curl.exe" }

if (-not (Test-Path $RepoPath)) { throw "RepoPath not found: $RepoPath" }
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $RepoPath "artifacts\green-gate" }
Ensure-Dir $OutDir

if ([string]::IsNullOrWhiteSpace($Token)) { $Token = [Environment]::GetEnvironmentVariable($TokenEnvVar) }
if (-not $LocalOnly -and [string]::IsNullOrWhiteSpace($Token)) { throw "Token required. Pass -Token or set env:$TokenEnvVar." }

Write-Header "GREEN GATE: Repo sanity"
cd $RepoPath
$st = (git status --porcelain)
if ($st -and $st.Trim().Length -gt 0) { throw "Working tree is not clean. Commit or stash.`n$st" }

$branch  = (git rev-parse --abbrev-ref HEAD).Trim()
$shaFull = (git rev-parse HEAD).Trim()
$sha7    = (git rev-parse --short=7 HEAD).Trim()
Write-Host "BRANCH = $branch"
Write-Host "HEAD   = $shaFull"
Write-Host "SHA7   = $sha7"

Write-Header "GREEN GATE: Local Truth Gate"
$truthGate = ".\scripts\truth-gate.ps1"
if (-not (Test-Path $truthGate)) { throw "Truth Gate script not found: $truthGate" }
& $truthGate

Write-Header "GREEN GATE: Extra RAG regression tests"
python -m pytest -q .\tests\test_rag_deterministic_signals_in_context.py
python -m pytest -q .\tests\test_rag_section_derived_risks.py
python -m pytest -q .\tests\test_rag_risk_materialization.py

if ($LocalOnly) { Write-Host "LocalOnly set -> stopping"; exit 0 }

Write-Header "GREEN GATE: Build + push image"
$env:AWS_PROFILE = $AwsProfile
$env:AWS_REGION  = $AwsRegion
$acct = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $acct) { throw "Failed to resolve AWS account id." }
$registry = "$acct.dkr.ecr.$AwsRegion.amazonaws.com"

if ([string]::IsNullOrWhiteSpace($ImageTagOverride)) { $tag = "aws-$sha7" } else { $tag = $ImageTagOverride.Trim() }
$img = "$registry/$EcrRepo`:$tag"

aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin $registry | Out-Null

if (-not [string]::IsNullOrWhiteSpace($ImageTagOverride)) {
  $repoName = $EcrRepo
  $ok = $true
  try { aws ecr describe-images --region $AwsRegion --repository-name $repoName --image-ids imageTag=$tag --output json | Out-Null } catch { $ok = $false }
  if (-not $ok) { throw ("ImageTagOverride not found in ECR. repo=" + $repoName + " tag=" + $tag) }
} else {
  $buildLog = Join-Path $OutDir "docker_build.log"
  $pushLog  = Join-Path $OutDir "docker_push.log"
  cmd.exe /c ("docker build -t ""$img"" . > ""$buildLog"" 2>&1")
  if ($LASTEXITCODE -ne 0) { throw "Docker build failed (exit=$LASTEXITCODE). See: $buildLog" }
  cmd.exe /c ("docker push ""$img"" > ""$pushLog"" 2>&1")
  if ($LASTEXITCODE -ne 0) { throw "Docker push failed (exit=$LASTEXITCODE). See: $pushLog" }
}

Write-Header "GREEN GATE: Helm deploy to css-mock"
$deployScript = ".\scripts\deploy-css-mock.ps1"
if (-not (Test-Path $deployScript)) { throw "Deploy script not found: $deployScript" }
$valuesPath = ".\deploy\helm\values-css-mock.yaml"
if (-not (Test-Path $valuesPath)) { throw "Expected helm values file missing: $valuesPath" }
try { & $deployScript -ImageTag $tag } finally { git restore $valuesPath | Out-Null }

Write-Header "GREEN GATE: Enforce single-image pods"
kubectl -n $Namespace rollout status "deploy/$Deployment"
$jsonpathPods = '{range .items[*]}{.metadata.name}{"|"}{.spec.containers[0].image}{"\n"}{end}'
$podMap = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath=$jsonpathPods
$podMapLines = ($podMap -split "`n" | Where-Object { $_.Trim().Length -gt 0 })
$bad = @()
foreach ($line in $podMapLines) { if ($line -notmatch [regex]::Escape(":$tag")) { $bad += $line } }
if ($bad.Count -gt 0) {
  foreach ($b in $bad) {
    $name = ($b -split "\|")[0].Trim()
    try { kubectl -n $Namespace delete pod $name | Out-Null } catch {}
  }
  kubectl -n $Namespace rollout status "deploy/$Deployment"
}

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
  $cmd = 'curl.exe -sS --fail-with-body -X POST "' + $url + '" ' +
         '-H "Authorization: Bearer ' + $Token + '" ' +
         '-H "Content-Type: application/json" ' +
         '--data-binary "@' + $payloadPath + '"'

  cmd.exe /c ($cmd + ' > "' + $respPath + '" 2>&1')
  if ($LASTEXITCODE -ne 0) {
    $preview = (Get-Content $respPath -TotalCount 80 | Out-String)
    throw ("curl failed (exit=" + $LASTEXITCODE + "):`n" + $preview)
  }

  $resp = Read-Json $respPath
  $warnings = @()
  if ($resp.warnings -is [System.Array]) { $warnings = @($resp.warnings) }
  elseif ($resp.warnings) { $warnings = @("$($resp.warnings)") }
  Write-Host ("warnings = " + ($warnings -join ", "))

  if ($warnings -contains "ingest_failed") { throw "ingest_failed" }
  if (-not $AllowPromptTruncated -and ($warnings -contains "prompt_truncated")) { throw "prompt_truncated" }

  $sections = @()
  if ($resp.sections -is [System.Array]) { $sections = @($resp.sections) }
  $totalEvidence = 0
  foreach ($s in $sections) { if ($s.evidence -is [System.Array]) { $totalEvidence += $s.evidence.Count } }

  $retrievedCountsTotal = 0
  if ($resp.retrieved_counts) { foreach ($pp in $resp.retrieved_counts.PSObject.Properties) { $retrievedCountsTotal += [int]$pp.Value } }

  Write-Host "sections = $($sections.Count)"
  Write-Host "retrieved_counts_total = $retrievedCountsTotal"
  Write-Host "totalEvidenceItems = $totalEvidence (min required = $MinEvidenceItems)"

  if ($totalEvidence -lt $MinEvidenceItems) { throw "evidence gate failed" }

  Write-Header "GREEN GATE: PASS"
  Write-Host "ImageTag   : $tag"
  Write-Host "Artifacts  : $OutDir"
}
catch {
  Dump-K8sDiagnostics -Ns $Namespace -Dep $Deployment -Selector $PodSelector -OutDir $OutDir
  throw
}
