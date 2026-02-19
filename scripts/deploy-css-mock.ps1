<#
.SYNOPSIS
  CSS Backend deployment SOURCE OF TRUTH for css-mock (GovCloud EKS).

.DESCRIPTION
  This script is the only supported way to deploy css-backend to the css-mock namespace.
  It uses Helm + a pinned values override file to avoid drift.

  Ownership boundaries:
    - Helm owns: Deployment, ConfigMap, ServiceAccount, Service
    - AWS Load Balancer Controller owns: TargetGroupBinding (created/managed from Ingress rules)

.PARAMETER Namespace
  Kubernetes namespace (default: css-mock)

.PARAMETER Release
  Helm release name (default: css-backend)

.PARAMETER ChartPath
  Path to chart (default: .\deploy\helm\css-backend)

.PARAMETER OverridePath
  Path to pinned override values file (default: .\deploy\helm\values-css-mock.yaml)

.PARAMETER ImageTag
  Explicit tag to deploy (ex: aws-3f6512b). If omitted, uses current git HEAD short sha: aws-<sha>.

.PARAMETER ReplicaCount
  Replicas for css-backend (default: 2)

.PARAMETER TimeoutMinutes
  Helm timeout in minutes (default: 10)

.PARAMETER SkipVerify
  Skip post-deploy verification checks

.EXAMPLE
  .\scripts\deploy-css-mock.ps1

.EXAMPLE
  .\scripts\deploy-css-mock.ps1 -ImageTag aws-3f6512b -ReplicaCount 2
#>

param(
  [string]$Namespace     = "css-mock",
  [string]$Release       = "css-backend",
  [string]$ChartPath     = ".\deploy\helm\css-backend",
  [string]$OverridePath  = ".\deploy\helm\values-css-mock.yaml",
  [string]$ImageTag,
  [int]$ReplicaCount     = 2,
  [int]$TimeoutMinutes   = 10,
  [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

function Require-Cmd([string]$name) {
  if (!(Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Missing required command '$name'. Install it and retry."
  }
}

function Get-GitTag() {
  $sha = (git rev-parse --short=7 HEAD 2>$null).Trim()
  if (!$sha) { throw "Could not determine git sha. Are you in a git repo?" }
  return "aws-$sha"
}

function Write-PinnedOverride([string]$path, [string]$tag, [int]$replicas) {
  $dir = Split-Path -Parent $path
  if (![string]::IsNullOrWhiteSpace($dir) -and !(Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
  }

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

  # Write UTF-8 no BOM
  [System.IO.File]::WriteAllText(
    (Resolve-Path (Split-Path -Parent $path)).Path + "\" + (Split-Path -Leaf $path),
    $yaml,
    (New-Object System.Text.UTF8Encoding($false))
  )
}

function Render-Helm([string]$chart, [string]$ns, [string]$override) {
  $render = & helm template $Release $chart -n $ns -f $override 2>&1
  if ($LASTEXITCODE -ne 0) { throw "helm template failed:`n$render" }
  return ($render | Out-String)
}

function Guardrails([string]$renderedYaml) {
  # No MinIO allowed in this environment
  if ($renderedYaml -match 'MINIO_' -or $renderedYaml -match 'MINIO_ENDPOINT') {
    throw "Guardrail violation: rendered manifests include MinIO env(s). css-mock must be S3 only."
  }
  if ($renderedYaml -match '(?m)^\s*STORAGE_(MODE|PROVIDER):\s*"(minio|s3-minio|local-minio)"\s*$') {
    throw "Guardrail violation: STORAGE_MODE/PROVIDER indicates MinIO. css-mock must be S3."
  }

  # Helm must NOT render TargetGroupBinding for css-mock
  if ($renderedYaml -match '(?m)^\s*kind:\s*TargetGroupBinding\s*$') {
    throw "Guardrail violation: chart is rendering TargetGroupBinding. For css-mock this must be owned by ALB controller. Keep targetGroupBinding.enabled=false."
  }

  # STORAGE_MODE must be s3 in rendered ConfigMap
  if ($renderedYaml -notmatch '(?m)^\s*STORAGE_MODE:\s*"s3"\s*$') {
    throw "Guardrail violation: STORAGE_MODE is not rendered as ""s3"". Fix chart values/configmap."
  }
}

function Verify-External([string]$baseUrl) {
  Write-Host "External checks:"
  $api  = try { (Invoke-WebRequest "$baseUrl/api/health" -UseBasicParsing).StatusCode } catch { $_.Exception.Response.StatusCode.Value__ }
  $root = try { (Invoke-WebRequest "$baseUrl/health" -UseBasicParsing).StatusCode } catch { $_.Exception.Response.StatusCode.Value__ }

  Write-Host ("  /api/health = {0}" -f $api)
  Write-Host ("  /health     = {0}" -f $root)

  if ($api -ne 200 -or $root -ne 200) {
    throw "External health checks failed. api=$api root=$root"
  }
}

function Verify-Cluster([string]$ns, [string]$release) {
  Write-Host "Cluster checks:"
  kubectl -n $ns get deploy $release -o wide
  kubectl -n $ns get pods -l app=$release -o wide
  kubectl -n $ns get svc $release -o wide
  kubectl -n $ns get endpoints $release -o wide

  Write-Host "Ingress route:"
  kubectl -n $ns describe ingress css-mock | Select-String -Pattern "/api|$release:8000" -Context 0,1

  Write-Host "TGB (ALB controller owned; name may change over time):"
  kubectl -n $ns get targetgroupbinding -o wide | Select-String -Pattern "cssbacke|css-backend"
}

# -----------------------------
# Main
# -----------------------------
Require-Cmd helm
Require-Cmd kubectl
Require-Cmd git

if (!(Test-Path $ChartPath)) { throw "ChartPath not found: $ChartPath" }

if (!$ImageTag) { $ImageTag = Get-GitTag }
Write-Host "Using ImageTag: $ImageTag"

Write-PinnedOverride -path $OverridePath -tag $ImageTag -replicas $ReplicaCount
Write-Host "Pinned override written: $OverridePath"
Get-Content $OverridePath | ForEach-Object { Write-Host ("  " + $_) }

Write-Host "Lint chart..."
helm lint $ChartPath | Out-Host

Write-Host "Render + guardrails..."
$rendered = Render-Helm -chart $ChartPath -ns $Namespace -override $OverridePath
Guardrails -renderedYaml $rendered

Write-Host "Deploy via Helm..."
$timeout = "{0}m" -f $TimeoutMinutes
helm upgrade $Release $ChartPath -n $Namespace -f $OverridePath --wait --timeout $timeout --rollback-on-failure | Out-Host

if (!$SkipVerify) {
  Verify-External -baseUrl "https://css-mock.shipcom.ai"
  Verify-Cluster -ns $Namespace -release $Release
}

Write-Host "DEPLOY OK"
