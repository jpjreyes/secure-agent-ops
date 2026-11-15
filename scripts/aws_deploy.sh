#!/usr/bin/env bash
# Build the image at the current commit, push it to ECR and point both Lambda functions at it (Terraform).
#   scripts/aws_deploy.sh            # needs a clean working tree; Colima for Docker; AWS credentials for the account
set -euo pipefail
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain)" ] || { echo "commit first: the image tag is the commit"; exit 1; }
TAG=$(git rev-parse --short HEAD)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sao-demo"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
# a private docker config, so the login doesn't go through another tool's credential helper
export DOCKER_CONFIG="${TMPDIR:-/tmp}/sao-docker-config"
mkdir -p "$DOCKER_CONFIG" && [ -f "$DOCKER_CONFIG/config.json" ] || echo '{}' > "$DOCKER_CONFIG/config.json"
echo "[1/4] build $TAG"
docker build --platform linux/arm64 --provenance=false -t "$REPO:$TAG" . >/dev/null
echo "[2/4] push"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%/*}" >/dev/null
docker push "$REPO:$TAG" >/dev/null
echo "[3/4] terraform apply (image_tag=$TAG)"
cd infra/aws
sed -i '' "s/^image_tag .*/image_tag   = \"$TAG\"/" terraform.tfvars
~/.local/bin/terraform apply -input=false -auto-approve >/dev/null
echo "[4/4] wait for the new API version (SnapStart snapshot)"
aws lambda wait function-active-v2 --function-name sao-demo-api --qualifier live --region "$REGION"
URL=$(~/.local/bin/terraform output -raw api_url)
curl -fsS "${URL}api/health" && echo && echo "deployed $TAG → $URL"
