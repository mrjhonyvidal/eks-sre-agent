#!/usr/bin/env bash
# Deploy the MCP gateway to AWS App Runner end-to-end.
# Idempotent: safe to re-run. Does NOT auto-deploy unless you set DEPLOY=1.
#
# Usage:
#   DEPLOY=1 ./scripts/setup.sh
#
# Requires: aws, docker, jq.
# Region/cluster default to the rest of the toolkit.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER_NAME:-eks-ai-ops}"
SERVICE="${MCP_SERVICE_NAME:-eks-ai-ops-mcp-gateway}"
ECR_REPO="${MCP_ECR_REPO:-eks-ai-ops-mcp-gateway}"
INSTANCE_ROLE="${MCP_INSTANCE_ROLE:-eks-ai-ops-mcp-instance}"
ACCESS_ROLE="${MCP_ACCESS_ROLE:-eks-ai-ops-mcp-ecr-access}"
SSM_URL_PARAM="${MCP_SSM_URL:-/eks-ai-ops-toolkit/mcp-gateway-url}"
SSM_KEY_PARAM="${MCP_SSM_KEY:-/eks-ai-ops-toolkit/mcp-gateway-api-key}"
API_KEY="${MCP_API_KEY:-$(openssl rand -hex 24)}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf "\033[36m▶ %s\033[0m\n" "$*"; }

if [[ "${DEPLOY:-0}" != "1" ]]; then
  log "Dry-run (set DEPLOY=1 to actually create resources)."
fi

run() {
  if [[ "${DEPLOY:-0}" == "1" ]]; then "$@"; else echo "  + $*"; fi
}

# ---------- 1. ECR repo ----------
log "Ensuring ECR repository ${ECR_REPO}"
run aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || run aws ecr create-repository --repository-name "${ECR_REPO}" --region "${REGION}" >/dev/null

# ---------- 2. Build & push image ----------
log "Building image (linux/arm64)"
run docker build --platform linux/arm64 -t "${ECR_REPO}:latest" "${HERE}"
log "Pushing to ${ECR_URI}"
run aws ecr get-login-password --region "${REGION}" | run docker login --username AWS --password-stdin "${ECR_URI%/*}"
run docker tag "${ECR_REPO}:latest" "${ECR_URI}:latest"
run docker push "${ECR_URI}:latest"

# ---------- 3. IAM roles ----------
log "Creating IAM instance role ${INSTANCE_ROLE} (App Runner task role)"
run aws iam create-role --role-name "${INSTANCE_ROLE}" \
  --assume-role-policy-document "file://${HERE}/iam/instance-trust-policy.json" 2>/dev/null || true
run aws iam put-role-policy --role-name "${INSTANCE_ROLE}" \
  --policy-name eks-ai-ops-mcp --policy-document "file://${HERE}/iam/instance-policy.json"

log "Creating IAM access role ${ACCESS_ROLE} (App Runner → ECR pull)"
run aws iam create-role --role-name "${ACCESS_ROLE}" \
  --assume-role-policy-document "file://${HERE}/iam/access-trust-policy.json" 2>/dev/null || true
run aws iam attach-role-policy --role-name "${ACCESS_ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true

INSTANCE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${INSTANCE_ROLE}"
ACCESS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ACCESS_ROLE}"

# ---------- 4. EKS access entry (cluster auth for the gateway) ----------
log "Granting MCP instance role read-only access to cluster ${CLUSTER}"
run aws eks create-access-entry --cluster-name "${CLUSTER}" --region "${REGION}" \
  --principal-arn "${INSTANCE_ROLE_ARN}" 2>/dev/null || true
run aws eks associate-access-policy --cluster-name "${CLUSTER}" --region "${REGION}" \
  --principal-arn "${INSTANCE_ROLE_ARN}" \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy \
  --access-scope type=cluster 2>/dev/null || true

# ---------- 5. App Runner service ----------
log "Creating App Runner service ${SERVICE}"
SOURCE_CONFIG=$(cat <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URI}:latest",
    "ImageRepositoryType": "ECR",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "AWS_REGION": "${REGION}",
        "EKS_CLUSTER_NAME": "${CLUSTER}",
        "MCP_GATEWAY_API_KEY": "${API_KEY}",
        "MCP_SERVERS": "eks=python -m awslabs.eks_mcp_server"
      }
    }
  },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": { "AccessRoleArn": "${ACCESS_ROLE_ARN}" }
}
JSON
)
INSTANCE_CONFIG="{\"Cpu\":\"0.25 vCPU\",\"Memory\":\"0.5 GB\",\"InstanceRoleArn\":\"${INSTANCE_ROLE_ARN}\"}"

if [[ "${DEPLOY:-0}" == "1" ]]; then
  if ! aws apprunner list-services --region "${REGION}" \
        --query "ServiceSummaryList[?ServiceName=='${SERVICE}'].ServiceArn" --output text \
        | grep -q apprunner; then
    aws apprunner create-service --region "${REGION}" \
      --service-name "${SERVICE}" \
      --source-configuration "${SOURCE_CONFIG}" \
      --instance-configuration "${INSTANCE_CONFIG}" >/dev/null
    log "Waiting for service to be RUNNING (this takes ~3–5 min)…"
  else
    log "Service exists; skipping create."
  fi
fi

# ---------- 6. SSM wiring ----------
SERVICE_ARN=$(aws apprunner list-services --region "${REGION}" \
    --query "ServiceSummaryList[?ServiceName=='${SERVICE}'].ServiceArn" --output text 2>/dev/null || true)
if [[ -n "${SERVICE_ARN}" && "${SERVICE_ARN}" != "None" ]]; then
  URL="https://$(aws apprunner describe-service --region "${REGION}" \
      --service-arn "${SERVICE_ARN}" --query 'Service.ServiceUrl' --output text)"
  log "Setting SSM ${SSM_URL_PARAM} = ${URL}"
  run aws ssm put-parameter --name "${SSM_URL_PARAM}" --type String --overwrite --value "${URL}" --region "${REGION}"
  log "Setting SSM ${SSM_KEY_PARAM}"
  run aws ssm put-parameter --name "${SSM_KEY_PARAM}" --type String --overwrite --value "${API_KEY}" --region "${REGION}"
  log "Done. Now redeploy the SAM stack so the bot Lambda picks up the new env vars:"
  echo "    make deploy"
else
  log "App Runner service not found yet (re-run with DEPLOY=1)."
fi
