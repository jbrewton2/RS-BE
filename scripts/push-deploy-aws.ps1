param(
  [string]$Namespace = "css-mock",
  [string]$AccountId = "354962495083",
  [string]$Region = "us-gov-east-1",
  [string]$EcrRepo = "css/css-backend",
  [string]$DeployName = "css-backend",
  [string]$ContainerName = "css-backend",
  [string]$AwsProfile = "css-gov",
  [string]$LocalImage = "rs-be:local"
)

$ErrorActionPreference="Stop"

# Ensure we are in repo root (script can be run from anywhere)
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

git status
git log -1 --oneline

# Ensure GitHub has the commit (origin is GitHub)
git push origin HEAD

# Build local image
docker build -t $LocalImage .

# AWS env
$env:AWS_PROFILE = $AwsProfile
$env:AWS_REGION = $Region
$env:AWS_SDK_LOAD_CONFIG = "1"

# Login to ECR
aws ecr get-login-password --region $env:AWS_REGION |
  docker login --username AWS --password-stdin "$AccountId.dkr.ecr.$Region.amazonaws.com" | Out-Null

# Tag = commit hash
$tag = "aws-$(git rev-parse --short HEAD)"
$remoteTag = "$AccountId.dkr.ecr.$Region.amazonaws.com/$EcrRepo`:$tag"

docker tag $LocalImage $remoteTag
docker push $remoteTag

# Resolve digest + deploy by digest
$digest = aws ecr describe-images `
  --repository-name $EcrRepo `
  --image-ids imageTag=$tag `
  --query "imageDetails[0].imageDigest" `
  --output text

if (-not $digest -or $digest -eq "None") { throw "Failed to resolve ECR digest for tag: $tag" }

$imgByDigest = "$AccountId.dkr.ecr.$Region.amazonaws.com/$EcrRepo@$digest"
Write-Host "DEPLOYING: $imgByDigest"

kubectl -n $Namespace set image "deploy/$DeployName" "$ContainerName=$imgByDigest"
kubectl -n $Namespace rollout status "deploy/$DeployName"

# Print the live image ID for proof
$pod = kubectl -n $Namespace get pods -l "app=$DeployName" -o jsonpath="{.items[0].metadata.name}"
kubectl -n $Namespace describe pod $pod | Select-String -Pattern "Image:|Image ID:"