<#
.SYNOPSIS
  CSS Backend deployment SOURCE OF TRUTH for css-mock (GovCloud EKS).

.DESCRIPTION
  This script is the only supported way to deploy css-backend to the css-mock namespace.
  It uses Helm + pinned values override to avoid drift.

  Ownership boundaries:
    - Helm owns: Deployment, ConfigMap, ServiceAccount, Service
    - AWS Load Balancer Controller owns: TargetGroupBinding (created via Ingress)

.PARAMETER Namespace
  Kubernetes namespace (default: css-mock)

.PARAMETER Release
  Helm release name (default: css-backend)

.PARAMETER ChartPath
  Path to chart (default: .\deploy\helm\css-backend)

.PARAMETER OverridePath
  Path to pinned override values file (default: .\deploy\helm\values-css-mock.yaml)

.PARAMETER ImageTag
  Explicit tag to deploy (ex: aws-648384e). If omitted, uses current git HEAD short sha: aws-<sha>.

.PARAMETER ReplicaCount
  Replicas for css-backend (default: 2)

.PARAMETER TimeoutMinutes
  Helm timeout in minutes (default: 10)

.PARAMETER SkipVerify
  Skip post-deploy verification checks

.PARAMETER BuildAndPush
  Build the backend Docker image locally and push it to ECR using the computed/current ImageTag before deploying.

.PARAMETER AutoBuildIfMissing
  If the requested ImageTag does not exist in ECR, automatically build+push that tag (same as -BuildAndPush) and then deploy.

.PARAMETER SkipEcrCheck
  Skip the ECR "tag exists" preflight check (not recommended)

.EXAMPLE
  cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"
  $env:AWS_PROFILE="css-gov"
  $env:AWS_REGION="us-gov-east-1"
  .\scripts\deploy-css-mock.ps1 -ImageTag aws-648384e
#>

param(
  [string]$Namespace = "css-mock",
  [string]$Release   = "css-backend",
      [string]$ChartPath = ".\deploy\helm\css-backend",

  # --- Frontend Helm deploy (optional) ---
  [switch]$DeployFrontend,
  [string]$FrontendTag = "",
  [string]$FrontendRelease = "css-frontend",
  [string]$FrontendChartPath = ".\deploy\helm\css-frontend",
  [string]$FrontendOverridePath = ".\deploy\helm\.rendered\values-css-frontend.pinned.yaml",

  # --- Frontend Helm deploy (optional) ---[string]$OverridePath = ".\deploy\helm\.rendered\values-css-mock.pinned.yaml",
  [string]$ImageTag,
  [int]$ReplicaCount = 2,
  [int]$TimeoutMinutes = 10,
  [switch]$SkipVerify,
  [switch]$BuildAndPush,
  [switch]$AutoBuildIfMissing,
  [switch]$SkipEcrCheck
)



function Set-YamlImageTagInPlace {
  param(
    [Parameter(Mandatory=$true)][string]$ValuesPath,
    [Parameter(Mandatory=$true)][string]$ImageTag
  )

  if (!(Test-Path $ValuesPath)) { throw "Values file not found: $ValuesPath" }

  $raw = Get-Content -Raw $ValuesPath

  if ($raw -notmatch '(?m)^\s*image:\s*$') {
    throw "values file missing 'image:' block: $ValuesPath"
  }

  if ($raw -match '(?m)^\s*tag:\s*".*"\s*$') {
    $raw = [regex]::Replace($raw, '(?m)^\s*tag:\s*".*"\s*$', ('  tag: "' + $ImageTag + '"'))
  } else {
    if ($raw -match '(?m)^\s*repository:\s*.+$') {
      $raw = [regex]::Replace($raw, '(?m)^\s*repository:\s*.+$', ('$0' + "`n" + '  tag: "' + $ImageTag + '"'))
    } else {
      $raw = [regex]::Replace($raw, '(?m)^\s*image:\s*$', ('$0' + "`n" + '  tag: "' + $ImageTag + '"'))
    }
  }

  if (-not $raw.EndsWith("`n")) { $raw += "`n" }

  # UTF-8 without BOM
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText((Resolve-Path $ValuesPath), $raw, $utf8NoBom)
}
$ErrorActionPreference="Stop"

# --- AWS credential behavior (local vs GitHub Actions) ---
# In GitHub Actions, credentials come from OIDC env vars. DO NOT force AWS_PROFILE.
# Locally, default to css-gov if no env creds are present.
try {
    $isGh = ($env:GITHUB_ACTIONS -eq "true")
    $hasEnvCreds = -not [string]::IsNullOrWhiteSpace($env:AWS_ACCESS_KEY_ID)
    if (-not $isGh -and -not $hasEnvCreds) {
        if ([string]::IsNullOrWhiteSpace($env:AWS_PROFILE)) {
            $env:AWS_PROFILE = "css-gov"
        }
    }
} catch { }
# -------------------------------------------------------

function Require-Cmd([string]$name) {
  if (!(Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Missing required command '$name'. Install it and retry."
  }
}

function Get-AwsExePath() {
  # Prefer AWS CLI v2 at standard install path; fall back to aws.exe on PATH.
  $v2 = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
  if (Test-Path $v2) { return $v2 }
  $cmd = (Get-Command aws.exe -ErrorAction SilentlyContinue)
  if ($cmd -and $cmd.Source) { return $cmd.Source }
  return "aws"
}
function Get-GitTag() {
  $sha = (git rev-parse --short=7 HEAD 2>$null).Trim()
  if (!$sha) { throw "Could not determine git sha. Are you in a git repo?" }
  return "aws-$sha"
}

function Write-PinnedOverride([string]$path, [string]$tag, [int]$replicas) {
  $dir = Split-Path -Parent $path
  if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

  $yaml = @"
# values-css-mock.yaml (pinned) - SOURCE OF TRUTH FOR THIS ENV
replicaCount: $replicas

image:
  repository: 354962495083.dkr.ecr.us-gov-east-1.amazonaws.com/css/css-backend
  tag: "$tag"
  pullPolicy: Always

# Ingress expects a Service. Keep enabled.
service:
  enabled: true

# AWS Load Balancer Controller owns TargetGroupBinding (Ingress-driven). Helm must NOT manage it here.
targetGroupBinding:
  enabled: false
"@

  $outPath = if ($dir) { Join-Path $dir (Split-Path -Leaf $path) } else { $path }
  [System.IO.File]::WriteAllText((Resolve-Path (Split-Path -Parent $outPath)).Path + "\" + (Split-Path -Leaf $outPath), $yaml, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-AwsCli([string[]]$AwsArgs) {
  # Run AWS CLI through cmd.exe so stderr does NOT become a PowerShell error record (EAP=Stop safe).
  $awsExe = (Get-Command "aws.exe" -ErrorAction SilentlyContinue)
  $exePath = if ($awsExe) { $awsExe.Source } else { "aws" }

  if (!$AwsArgs -or $AwsArgs.Count -eq 0) {
    throw "Invoke-AwsCli called with no arguments. This is a script bug."
  }

  $escapedArgs = $AwsArgs | ForEach-Object { '"' + ($_ -replace '"','\"') + '"' }
  $cmdLine = '"' + ($exePath -replace '"','\"') + '" ' + ($escapedArgs -join ' ')

  $out = cmd.exe /c "$cmdLine 2>&1"
  $code = $LASTEXITCODE

  if ($code -ne 0) {
    throw ("AWS CLI failed (exit {0}): {1}`n{2}" -f $code, $cmdLine, ($out | Out-String))
  }

  return ($out | Out-String)
}

function Verify-EcrTagExists([string]$RepoName, [string]$Tag, [string]$Region) {
  if ($SkipEcrCheck) {
    Write-Host "ECR check: SKIPPED (SkipEcrCheck set)" -ForegroundColor Yellow
    return
  }

  if (![string]::IsNullOrWhiteSpace($env:AWS_PROFILE)) { $profile = $env:AWS_PROFILE } else { $profile = "<default>" }
  Write-Host "ECR check: ensuring tag exists: $RepoName`:$Tag (region=$Region, profile=$profile)" -ForegroundColor Cyan

  # IMPORTANT: pass a real string[] of args (no $args ambiguity)
  $cmd = @(
    "ecr", "describe-images",
    "--region", $Region,
    "--repository-name", $RepoName,
    "--image-ids", "imageTag=$Tag"
  )

  $null = Invoke-AwsCli -AwsArgs $cmd
}

function Render-Helm([string]$chart, [string]$ns, [string]$override) {
  $render = & helm template $Release $chart -n $ns -f $override 2>&1
  if ($LASTEXITCODE -ne 0) { throw "helm template failed:`n$render" }
  return ($render | Out-String)
}

function Guardrails([string]$renderedYaml) {
  if ($renderedYaml -match 'MINIO_' -or $renderedYaml -match 'MINIO_ENDPOINT') {
    throw "Guardrail violation: rendered manifests include MinIO env(s). This env must be S3 only."
  }
  if ($renderedYaml -match '(?m)^\s*kind:\s*TargetGroupBinding\s*$') {
    throw "Guardrail violation: chart is rendering TargetGroupBinding. For css-mock this must be owned by ALB controller. Keep targetGroupBinding.enabled=false."
  }
  if ($renderedYaml -notmatch '(?m)^\s*STORAGE_MODE:\s*"s3"\s*$') {
    throw "Guardrail violation: STORAGE_MODE is not rendered as ""s3"". Fix chart values/configmap."
  }
}

function Verify-External([string]$baseUrl) {
  Write-Host "External checks:" -ForegroundColor Cyan
  $api  = try { (Invoke-WebRequest "$baseUrl/api/health" -UseBasicParsing).StatusCode } catch { $_.Exception.Response.StatusCode.Value__ }
  $root = try { (Invoke-WebRequest "$baseUrl/health" -UseBasicParsing).StatusCode } catch { $_.Exception.Response.StatusCode.Value__ }
  Write-Host "  /api/health = $api"
  Write-Host "  /health     = $root"
  if ($api -ne 200 -or $root -ne 200) { throw "External health checks failed. api=$api root=$root" }
}

function Verify-Cluster([string]$ns, [string]$release) {
  Write-Host "Cluster checks:" -ForegroundColor Cyan
  & kubectl -n $ns get deploy $release -o wide
  & kubectl -n $ns get pods -l app=$release -o wide
  & kubectl -n $ns get svc $release -o wide
  & kubectl -n $ns get endpoints $release -o wide

  Write-Host "Ingress route:" -ForegroundColor Cyan
  & kubectl -n $ns describe ingress css-mock | Select-String -Pattern "/api|$release:8000" -Context 0,1

  Write-Host "TGB (ALB controller owned; name may change over time):" -ForegroundColor Cyan
  & kubectl -n $ns get targetgroupbinding -o wide | Select-String -Pattern "cssbacke|css-backend"
}

# --- Main ---
Require-Cmd helm
Require-Cmd kubectl
Require-Cmd git

if (!(Test-Path $ChartPath)) { throw "ChartPath not found: $ChartPath" }

if (!$ImageTag) { $ImageTag = Get-GitTag }
Write-Host "Using ImageTag: $ImageTag"

  if ($BuildAndPush) {
    $bpRegion  = if (![string]::IsNullOrWhiteSpace($env:AWS_REGION)) { $env:AWS_REGION } else { "us-gov-east-1" }
    $bpProfile = if (![string]::IsNullOrWhiteSpace($env:AWS_PROFILE)) { $env:AWS_PROFILE } else { "css-gov" }
    Write-Host "BuildAndPush: AWS_PROFILE=$bpProfile AWS_REGION=$bpRegion" -ForegroundColor DarkGray
    Write-Host "BuildAndPush: building docker image and pushing to ECR..." -ForegroundColor Cyan

    # NOTE: Repo is fixed for this environment
    $acct = "354962495083"
    $repoName = "css/css-backend"
    $repoHost = "$acct.dkr.ecr.$bpRegion.amazonaws.com"
    $repo = "$repoHost/$repoName"

    # ECR login
    $awsCmd = (Get-Command aws.exe -ErrorAction SilentlyContinue)
    $exePath = if ($awsCmd) { $awsCmd.Source } else { "aws" }
    Write-Host "BuildAndPush: AWS_EXE=$exePath" -ForegroundColor DarkGray
    $pw = & $exePath --profile $bpProfile --region $bpRegion ecr get-login-password
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pw)) { throw "ECR get-login-password failed (exit $LASTEXITCODE)" }
    $pw | docker login --username AWS --password-stdin $repoHost
    if ($LASTEXITCODE -ne 0) { throw "ECR login failed (exit $LASTEXITCODE)" }

    # Build, tag, push
    docker build -t "css-backend:$ImageTag" .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed (exit $LASTEXITCODE)" }

    docker tag "css-backend:$ImageTag" "${repo}:$ImageTag"
    if ($LASTEXITCODE -ne 0) { throw "docker tag failed (exit $LASTEXITCODE)" }

    docker push "${repo}:$ImageTag"
    if ($LASTEXITCODE -ne 0) { throw "docker push failed (exit $LASTEXITCODE)" }

    Write-Host "BuildAndPush: push complete: ${repo}:$ImageTag" -ForegroundColor Green
  }

# Region preference order: AWS_REGION env var -> default us-gov-east-1
$region = if (![string]::IsNullOrWhiteSpace($env:AWS_REGION)) { $env:AWS_REGION } else { "us-gov-east-1" }
    $ProfileArgs = @(); if (![string]::IsNullOrWhiteSpace($env:AWS_PROFILE)) { $ProfileArgs = @("--profile",$env:AWS_PROFILE) }

# ECR preflight (repo name is fixed for this env)
try {
  Verify-EcrTagExists -RepoName "css/css-backend" -Tag $ImageTag -Region $region
} catch {
  if ($AutoBuildIfMissing) {
    Write-Host "ECR check: tag missing; AutoBuildIfMissing enabled -> building/pushing $ImageTag" -ForegroundColor Yellow
    $script:BuildAndPush = $true
  } else {
    throw
  }
}

# If AutoBuildIfMissing flipped BuildAndPush on, do the build+push NOW (before Helm deploy)
if ($BuildAndPush) {
  if ($AutoBuildIfMissing -and $BuildAndPush) {
    Write-Host "AutoBuildIfMissing: executing BuildAndPush before Helm deploy..." -ForegroundColor Yellow
  }
}

# Ensure override directory exists (avoid mutating tracked files; write to rendered path)
# Guard: OverridePath must be set (pinned backend values file)
if ([string]::IsNullOrWhiteSpace($OverridePath)) {
  $OverridePath = ".\deploy\helm\.rendered\values-css-mock.pinned.yaml"
}
$ovDir = Split-Path -Parent $OverridePath
if (![string]::IsNullOrWhiteSpace($ovDir) -and !(Test-Path $ovDir)) { New-Item -ItemType Directory -Path $ovDir | Out-Null }
if (!(Test-Path $OverridePath)) {
  Write-Host "Pinned override missing; creating: $OverridePath" -ForegroundColor Yellow
  Write-PinnedOverride -path $OverridePath -tag $ImageTag -replicas $ReplicaCount
} else {
  Write-Host "Pinned override in use: $OverridePath" -ForegroundColor Cyan
  Set-YamlImageTagInPlace -ValuesPath $OverridePath -ImageTag $ImageTag
}
Write-Host "Pinned override written: $OverridePath"
Get-Content $OverridePath | ForEach-Object { "  $_" }

Write-Host "Lint chart..." -ForegroundColor Cyan
& helm lint $ChartPath

Write-Host "Render + guardrails..." -ForegroundColor Cyan
$rendered = Render-Helm -chart $ChartPath -ns $Namespace -override $OverridePath
Guardrails -renderedYaml $rendered

Write-Host "Deploy via Helm..." -ForegroundColor Cyan
$timeout = "{0}m" -f $TimeoutMinutes
& helm upgrade $Release $ChartPath -n $Namespace -f $OverridePath --atomic --timeout $timeout
if ($LASTEXITCODE -ne 0) { throw "helm upgrade failed ($LASTEXITCODE)" }

if (!$SkipVerify) {
  Verify-External -baseUrl "https://css-mock.shipcom.ai"
  Verify-Cluster -ns $Namespace -release $Release
}

# ------------------------------------------------------------
# Optional: deploy frontend via Helm (separate release)
# ------------------------------------------------------------
if ($DeployFrontend) {
  if ([string]::IsNullOrWhiteSpace($FrontendTag)) { throw "DeployFrontend set but FrontendTag is empty (expected like web-<sha>)" }
  if (!(Test-Path $FrontendChartPath)) { throw "Missing FrontendChartPath: $FrontendChartPath" }
  if (!(Test-Path $FrontendOverridePath)) { throw "Missing FrontendOverridePath: $FrontendOverridePath" }

  Write-Host "Frontend deploy: release=$FrontendRelease tag=$FrontendTag" -ForegroundColor Cyan

  # Ensure override dir exists
  $fovDir = Split-Path -Parent $FrontendOverridePath
  if (![string]::IsNullOrWhiteSpace($fovDir) -and !(Test-Path $fovDir)) { New-Item -ItemType Directory -Path $fovDir | Out-Null }

  # Reuse existing YAML tag setter (image.tag)
  Set-YamlImageTagInPlace -ValuesPath $FrontendOverridePath -ImageTag $FrontendTag

  Write-Host "Frontend pinned override written: $FrontendOverridePath" -ForegroundColor DarkGray
  Get-Content $FrontendOverridePath | ForEach-Object { "  $_" }

  Write-Host "Deploying frontend via Helm..." -ForegroundColor Cyan
  & helm upgrade --install $FrontendRelease $FrontendChartPath -n $Namespace -f $FrontendOverridePath --atomic --timeout $timeout

  Write-Host "Waiting for frontend rollout..." -ForegroundColor Cyan
  & kubectl -n $Namespace rollout status ("deploy/" + $FrontendRelease) --timeout=180s | Out-Host
}
Write-Host "DEPLOY OK" -ForegroundColor Green





