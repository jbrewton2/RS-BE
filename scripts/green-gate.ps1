# scripts/green-gate.ps1
# PURPOSE:
#   "Green Gate" = deterministic, repeatable pipeline to get RS-BE (css-backend) to a known-good state
#   and validate live behavior in css-mock (EKS) with hard guardrails:
#     - Truth Gate (compile + unit tests + drift guards)
#     - build + push ECR image from current repo HEAD (or use -ImageTagOverride after validating exists)
#     - Helm deploy to css-mock
#     - enforce single-image pods
#     - live /api/rag/analyze validation:
#         - warnings must NOT include ingest_failed
#         - warnings must NOT include prompt_truncated (unless -AllowPromptTruncated)
#         - sections[*].evidence must contain at least N items (configurable)
#         - at least one evidence item must be "usable" for open-doc: (doc|docId) + charStart+charEnd
#
# NOTES:
#   - This script does NOT commit values-css-mock.yaml. It restores it after deployment.
#   - Requires: git, python, aws cli, kubectl, helm, docker, curl.exe (Windows)
#   - Run from anywhere; it always cd's to the repo path.

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [string]$RepoPath,

  [Parameter(Mandatory=$true)]
  [string]$AwsProfile,

  [Parameter(Mandatory=$true)]
  [string]$AwsRegion,

  [Parameter(Mandatory=$true)]
  [string]$EcrRepo, # ex: "css/css-backend"

  [Parameter(Mandatory=$true)]
  [string]$Namespace, # ex: "css-mock"

  [Parameter(Mandatory=$true)]
  [string]$Deployment, # ex: "css-backend" (k8s deployment name)

  # IMPORTANT: label selector for pods. Do not assume it always matches deployment name.
  [string]$PodSelector,

  [Parameter(Mandatory=$true)]
  [string]$BaseUrl, # ex: "https://css-mock.shipcom.ai"

  [Parameter(Mandatory=$true)]
  [string]$Token, # Cognito access token

  [Parameter(Mandatory=$true)]
  [string]$ReviewId, # review guid

  [ValidateSet("fast","balanced","deep")]
  [string]$ContextProfile="deep",

  [ValidateSet("risk_triage","strict_summary")]
  [string]$AnalysisIntent="risk_triage",

  [int]$TopK=12,

  [switch]$ForceReingest,

  # Hard gate: at least this many evidence items across ALL sections
  [int]$MinEvidenceItems=1,

  # Default: prompt_truncated is a FAIL. Flip this if you want to allow it temporarily.
  [switch]$AllowPromptTruncated,

  # Optional: skip build/push/deploy if you only want to run local gates.
  [switch]$LocalOnly,

  # Optional: use a prebuilt tag (skips docker build/push, but still deploys). MUST exist in ECR.
  [string]$ImageTagOverride,

  # Where to write artifacts (rag json, logs)
  [string]$OutDir
)

$ErrorActionPreference = "Stop"

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
  return $raw | ConvertFrom-Json
}

function To-JsonFile($Obj, [string]$Path) {
  $Obj | ConvertTo-Json -Depth 100 | Out-File -Encoding utf8 $Path
}

function Dump-K8sDiagnostics {
  param(
    [string]$Ns,
    [string]$Dep,
    [string]$Selector,
    [string]$OutDir
  )

  try {
    Ensure-Dir $OutDir

    $descPath = Join-Path $OutDir "k8s_describe_deploy.txt"
    $podsPath = Join-Path $OutDir "k8s_pods.txt"
    $logPath  = Join-Path $OutDir "k8s_backend_logs_tail.txt"

    kubectl -n $Ns describe "deploy/$Dep" | Out-File -Encoding utf8 $descPath
    kubectl -n $Ns get pods -l $Selector -o wide | Out-File -Encoding utf8 $podsPath

    # Tail logs for the newest matching pod
    $pod = (kubectl -n $Ns get pods -l $Selector --sort-by=.metadata.creationTimestamp -o jsonpath="{.items[-1:].metadata.name}")
    if ($pod) {
      kubectl -n $Ns logs $pod --tail=400 | Out-File -Encoding utf8 $logPath
    }
  } catch {
    Write-Host "Diagnostics dump failed (non-fatal). Error: $($_.Exception.Message)"
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

if ([string]::IsNullOrWhiteSpace($PodSelector)) {
  # Default is explicit but still derived. Override when labels differ.
  $PodSelector = "app=$Deployment"
}

# -----------------------------
# Move to repo
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

# Fail fast on common ReviewId mistakes (blank or placeholder).
if ([string]::IsNullOrWhiteSpace($ReviewId)) {
  throw "ReviewId is required and cannot be blank."
}
if ($ReviewId.Trim() -match '^\<.*review.*\>
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
# Build + push image (unless overridden)
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

aws ecr get-login-password --region $AwsRegion |
  docker login --username AWS --password-stdin $registry | Out-Null

if (-not [string]::IsNullOrWhiteSpace($ImageTagOverride)) {
  $repoName = $EcrRepo
  $tagCheck = $null
  try {
    $tagCheck = aws ecr describe-images --region $AwsRegion --repository-name $repoName --image-ids imageTag=$tag --output json
  } catch {
    $tagCheck = $null
  }
  if (-not $tagCheck) {
    throw ("ImageTagOverride tag not found in ECR repo. repo=" + $repoName + " tag=" + $tag + " region=" + $AwsRegion)
  }
  Write-Host ("ECR tag exists: " + $repoName + ":" + $tag)
} else {
  $buildLog = Join-Path $OutDir "docker_build.log"
  $pushLog  = Join-Path $OutDir "docker_push.log"

  # Docker/BuildKit often writes progress to stderr; don't let PowerShell treat that as failure.
  cmd.exe /c "docker build -t `"$img`" . > `"$buildLog`" 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "Docker build failed (exit=$LASTEXITCODE). See: $buildLog" }

  cmd.exe /c "docker push `"$img`" > `"$pushLog`" 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "Docker push failed (exit=$LASTEXITCODE). See: $pushLog" }
}

# -----------------------------
# Helm deploy (scripted) + restore pinned values
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

$podMap = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath="{range .items[*]}{.metadata.name}{'|'}{.spec.containers[0].image}{'\n'}{end}"
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
    kubectl -n $Namespace delete pod $name | Out-Null
  }
  kubectl -n $Namespace rollout status "deploy/$Deployment"
}

$podMap2 = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath="{range .items[*]}{.metadata.name}{'|'}{.spec.containers[0].image}{'\n'}{end}"
Write-Host $podMap2

$stillBad = @()
foreach ($line in (($podMap2 -split "`n") | Where-Object { $_.Trim().Length -gt 0 })) {
  if ($line -notmatch [regex]::Escape(":$tag")) { $stillBad += $line }
}
if ($stillBad.Count -gt 0) {
  throw "Mixed images remain after enforcement. Expected tag :$tag but found:`n$($stillBad -join "`n")"
}

# -----------------------------
# Live API validation (/api/rag/analyze)
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

  Write-Host "POST $BaseUrl/api/rag/analyze"
  Write-Host "payload: $payloadPath"
  Write-Host "resp:    $respPath"

  # IMPORTANT: do NOT pipe curl into Tee-Object.
  # Pipelines can mask curl exit codes in Windows PowerShell. Use cmd.exe redirection and check exit code.
  $url = "$BaseUrl/api/rag/analyze"
  $cmd = 'curl.exe -sS --fail-with-body -X POST "' + $url + '" ' +
         '-H "Authorization: Bearer ' + $Token + '" ' +
         '-H "Content-Type: application/json" ' +
         '--data-binary "@' + $payloadPath + '"'

  cmd.exe /c ($cmd + ' > "' + $respPath + '" 2>&1')
  if ($LASTEXITCODE -ne 0) {
    $preview = ""
    try { $preview = (Get-Content $respPath -TotalCount 60 | Out-String) } catch { $preview = "<unable to read response file>" }
    throw ("GREEN GATE FAIL: curl returned non-zero exit code: " + $LASTEXITCODE +
           ". Response preview (first lines):`n" + $preview)
  }

  $resp = Read-Json $respPath
  # HARDENING: fail fast with a clear message when the review has no docs.
  # This prevents wasting time thinking "evidence attach" is broken when ingest never ran.
  $ing = $null
  if ($resp.stats -and $resp.stats.ingest) { $ing = $resp.stats.ingest }
  if ($ing -and $ing.reason -eq "no_docs") {
    throw ("GREEN GATE FAIL: review has no docs (stats.ingest.reason=no_docs). " +
           "Check ReviewId and confirm docs exist in metadata store for this review. " +
           "ingest=" + ($ing | ConvertTo-Json -Depth 20))
  }

  $warnings = @()
  if ($resp.warnings -is [System.Array]) { $warnings = @($resp.warnings) }
  elseif ($resp.warnings) { $warnings = @("$($resp.warnings)") }

  Write-Host ("warnings = " + ($warnings -join ", "))

  if ($warnings -contains "ingest_failed") {
    $ing = $resp.stats.ingest
    $ingStr = ""
    if ($ing) { $ingStr = ($ing | ConvertTo-Json -Depth 50) }
    throw "GREEN GATE FAIL: warnings includes ingest_failed. ingest stats: $ingStr"
  }

  if (-not $AllowPromptTruncated) {
    if ($warnings -contains "prompt_truncated") {
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
  }

  $sections = @()
  if ($resp.sections -is [System.Array]) { $sections = @($resp.sections) }

  $totalEvidence = 0
  foreach ($s in $sections) {
    $ev = $s.evidence
    if ($ev -is [System.Array]) { $totalEvidence += $ev.Count }
  }

  $retrievedCountsTotal = 0
  if ($resp.retrieved_counts) {
    foreach ($p in $resp.retrieved_counts.PSObject.Properties) {
      $retrievedCountsTotal += [int]$p.Value
    }
  }

  Write-Host "sections = $($sections.Count)"
  Write-Host "retrieved_counts_total = $retrievedCountsTotal"
  Write-Host "totalEvidenceItems = $totalEvidence (min required = $MinEvidenceItems)"

  if ($totalEvidence -lt $MinEvidenceItems) {
    if ($retrievedCountsTotal -gt 0) {
      throw "GREEN GATE FAIL: retrieval succeeded (retrieved_counts_total=$retrievedCountsTotal) but evidence attachment is empty. This is an attach/normalize bug, not retrieval."
    }

    $rd = $resp.stats.retrieval_debug
    $rdTop = $null
    if ($rd -is [System.Array] -and $rd.Count -gt 0) { $rdTop = $rd[0] }
    $rdJson = ""
    if ($rdTop) { $rdJson = ($rdTop | ConvertTo-Json -Depth 20) }

    throw ("GREEN GATE FAIL: retrieval returned zero hits (retrieved_counts_total=0), so evidence is empty. " +
       "Check ingest + vector store population. " +
       "stats.ingest=" + (($resp.stats.ingest) | ConvertTo-Json -Depth 20) + "; " +
       "stats.retrieved_total=" + $resp.stats.retrieved_total + "; " +
       "retrieval_debug[0]=" + $rdJson)
  }

  $hasUsableEvidence = $false
  foreach ($s in $sections) {
    $ev = $s.evidence
    if (-not ($ev -is [System.Array])) { continue }

    foreach ($e in $ev) {
      $docOk  = ($null -ne $e.docId -and $e.docId.ToString().Trim().Length -gt 0) -or
                ($null -ne $e.doc   -and $e.doc.ToString().Trim().Length -gt 0)
      $spanOk = ($null -ne $e.charStart) -and ($null -ne $e.charEnd)

      if ($docOk -and $spanOk) { $hasUsableEvidence = $true; break }
    }

    if ($hasUsableEvidence) { break }
  }

  if (-not $hasUsableEvidence) {
    throw "GREEN GATE FAIL: evidence exists but is not usable for open-doc (requires doc/docId + charStart/charEnd on at least one item)."
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






) {
  throw "ReviewId looks like a placeholder ($ReviewId). Provide a real review GUID."
}

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
# Build + push image (unless overridden)
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

aws ecr get-login-password --region $AwsRegion |
  docker login --username AWS --password-stdin $registry | Out-Null

if (-not [string]::IsNullOrWhiteSpace($ImageTagOverride)) {
  $repoName = $EcrRepo
  $tagCheck = $null
  try {
    $tagCheck = aws ecr describe-images --region $AwsRegion --repository-name $repoName --image-ids imageTag=$tag --output json
  } catch {
    $tagCheck = $null
  }
  if (-not $tagCheck) {
    throw ("ImageTagOverride tag not found in ECR repo. repo=" + $repoName + " tag=" + $tag + " region=" + $AwsRegion)
  }
  Write-Host ("ECR tag exists: " + $repoName + ":" + $tag)
} else {
  $buildLog = Join-Path $OutDir "docker_build.log"
  $pushLog  = Join-Path $OutDir "docker_push.log"

  # Docker/BuildKit often writes progress to stderr; don't let PowerShell treat that as failure.
  cmd.exe /c "docker build -t `"$img`" . > `"$buildLog`" 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "Docker build failed (exit=$LASTEXITCODE). See: $buildLog" }

  cmd.exe /c "docker push `"$img`" > `"$pushLog`" 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "Docker push failed (exit=$LASTEXITCODE). See: $pushLog" }
}

# -----------------------------
# Helm deploy (scripted) + restore pinned values
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

$podMap = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath="{range .items[*]}{.metadata.name}{'|'}{.spec.containers[0].image}{'\n'}{end}"
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
    kubectl -n $Namespace delete pod $name | Out-Null
  }
  kubectl -n $Namespace rollout status "deploy/$Deployment"
}

$podMap2 = kubectl -n $Namespace get pods -l $PodSelector -o jsonpath="{range .items[*]}{.metadata.name}{'|'}{.spec.containers[0].image}{'\n'}{end}"
Write-Host $podMap2

$stillBad = @()
foreach ($line in (($podMap2 -split "`n") | Where-Object { $_.Trim().Length -gt 0 })) {
  if ($line -notmatch [regex]::Escape(":$tag")) { $stillBad += $line }
}
if ($stillBad.Count -gt 0) {
  throw "Mixed images remain after enforcement. Expected tag :$tag but found:`n$($stillBad -join "`n")"
}

# -----------------------------
# Live API validation (/api/rag/analyze)
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

  Write-Host "POST $BaseUrl/api/rag/analyze"
  Write-Host "payload: $payloadPath"
  Write-Host "resp:    $respPath"

  # IMPORTANT: do NOT pipe curl into Tee-Object.
  # Pipelines can mask curl exit codes in Windows PowerShell. Use cmd.exe redirection and check exit code.
  $url = "$BaseUrl/api/rag/analyze"
  $cmd = 'curl.exe -sS --fail-with-body -X POST "' + $url + '" ' +
         '-H "Authorization: Bearer ' + $Token + '" ' +
         '-H "Content-Type: application/json" ' +
         '--data-binary "@' + $payloadPath + '"'

  cmd.exe /c ($cmd + ' > "' + $respPath + '" 2>&1')
  if ($LASTEXITCODE -ne 0) {
    $preview = ""
    try { $preview = (Get-Content $respPath -TotalCount 60 | Out-String) } catch { $preview = "<unable to read response file>" }
    throw ("GREEN GATE FAIL: curl returned non-zero exit code: " + $LASTEXITCODE +
           ". Response preview (first lines):`n" + $preview)
  }

  $resp = Read-Json $respPath
  # HARDENING: fail fast with a clear message when the review has no docs.
  # This prevents wasting time thinking "evidence attach" is broken when ingest never ran.
  $ing = $null
  if ($resp.stats -and $resp.stats.ingest) { $ing = $resp.stats.ingest }
  if ($ing -and $ing.reason -eq "no_docs") {
    throw ("GREEN GATE FAIL: review has no docs (stats.ingest.reason=no_docs). " +
           "Check ReviewId and confirm docs exist in metadata store for this review. " +
           "ingest=" + ($ing | ConvertTo-Json -Depth 20))
  }

  $warnings = @()
  if ($resp.warnings -is [System.Array]) { $warnings = @($resp.warnings) }
  elseif ($resp.warnings) { $warnings = @("$($resp.warnings)") }

  Write-Host ("warnings = " + ($warnings -join ", "))

  if ($warnings -contains "ingest_failed") {
    $ing = $resp.stats.ingest
    $ingStr = ""
    if ($ing) { $ingStr = ($ing | ConvertTo-Json -Depth 50) }
    throw "GREEN GATE FAIL: warnings includes ingest_failed. ingest stats: $ingStr"
  }

  if (-not $AllowPromptTruncated) {
    if ($warnings -contains "prompt_truncated") {
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
  }

  $sections = @()
  if ($resp.sections -is [System.Array]) { $sections = @($resp.sections) }

  $totalEvidence = 0
  foreach ($s in $sections) {
    $ev = $s.evidence
    if ($ev -is [System.Array]) { $totalEvidence += $ev.Count }
  }

  $retrievedCountsTotal = 0
  if ($resp.retrieved_counts) {
    foreach ($p in $resp.retrieved_counts.PSObject.Properties) {
      $retrievedCountsTotal += [int]$p.Value
    }
  }

  Write-Host "sections = $($sections.Count)"
  Write-Host "retrieved_counts_total = $retrievedCountsTotal"
  Write-Host "totalEvidenceItems = $totalEvidence (min required = $MinEvidenceItems)"

  if ($totalEvidence -lt $MinEvidenceItems) {
    if ($retrievedCountsTotal -gt 0) {
      throw "GREEN GATE FAIL: retrieval succeeded (retrieved_counts_total=$retrievedCountsTotal) but evidence attachment is empty. This is an attach/normalize bug, not retrieval."
    }

    $rd = $resp.stats.retrieval_debug
    $rdTop = $null
    if ($rd -is [System.Array] -and $rd.Count -gt 0) { $rdTop = $rd[0] }
    $rdJson = ""
    if ($rdTop) { $rdJson = ($rdTop | ConvertTo-Json -Depth 20) }

    throw ("GREEN GATE FAIL: retrieval returned zero hits (retrieved_counts_total=0), so evidence is empty. " +
       "Check ingest + vector store population. " +
       "stats.ingest=" + (($resp.stats.ingest) | ConvertTo-Json -Depth 20) + "; " +
       "stats.retrieved_total=" + $resp.stats.retrieved_total + "; " +
       "retrieval_debug[0]=" + $rdJson)
  }

  $hasUsableEvidence = $false
  foreach ($s in $sections) {
    $ev = $s.evidence
    if (-not ($ev -is [System.Array])) { continue }

    foreach ($e in $ev) {
      $docOk  = ($null -ne $e.docId -and $e.docId.ToString().Trim().Length -gt 0) -or
                ($null -ne $e.doc   -and $e.doc.ToString().Trim().Length -gt 0)
      $spanOk = ($null -ne $e.charStart) -and ($null -ne $e.charEnd)

      if ($docOk -and $spanOk) { $hasUsableEvidence = $true; break }
    }

    if ($hasUsableEvidence) { break }
  }

  if (-not $hasUsableEvidence) {
    throw "GREEN GATE FAIL: evidence exists but is not usable for open-doc (requires doc/docId + charStart/charEnd on at least one item)."
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










