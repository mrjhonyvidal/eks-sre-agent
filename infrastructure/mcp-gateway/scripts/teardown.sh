#!/usr/bin/env bash
# Tear down everything created by setup.sh.
# Safe to re-run. Asks for confirmation unless FORCE=1.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER_NAME:-eks-ai-ops}"
SERVICE="${MCP_SERVICE_NAME:-eks-ai-ops-mcp-gateway}"
ECR_REPO="${MCP_ECR_REPO:-eks-ai-ops-mcp-gateway}"
INSTANCE_ROLE="${MCP_INSTANCE_ROLE:-eks-ai-ops-mcp-instance}"
ACCESS_ROLE="${MCP_ACCESS_ROLE:-eks-ai-ops-mcp-ecr-access}"
SSM_URL_PARAM="${MCP_SSM_URL:-/eks-ai-ops-toolkit/mcp-gateway-url}"
SSM_KEY_PARAM="${MCP_SSM_KEY:-/eks-ai-ops-toolkit/mcp-gateway-api-key}"

log() { printf "\033[36m▶ %s\033[0m\n" "$*"; }

if [[ "${FORCE:-0}" != "1" ]]; then
  read -r -p "Delete MCP gateway resources in ${REGION}? [y/N] " yn
  [[ "${yn}" == "y" || "${yn}" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
INSTANCE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${INSTANCE_ROLE}"

log "Deleting App Runner service ${SERVICE}"
SERVICE_ARN=$(aws apprunner list-services --region "${REGION}" \
    --query "ServiceSummaryList[?ServiceName=='${SERVICE}'].ServiceArn" --output text 2>/dev/null || true)
if [[ -n "${SERVICE_ARN}" && "${SERVICE_ARN}" != "None" ]]; then
  aws apprunner delete-service --service-arn "${SERVICE_ARN}" --region "${REGION}" >/dev/null || true
fi

log "Removing EKS access entry for ${INSTANCE_ROLE_ARN}"
aws eks delete-access-entry --cluster-name "${CLUSTER}" --region "${REGION}" \
  --principal-arn "${INSTANCE_ROLE_ARN}" 2>/dev/null || true

log "Deleting IAM role ${INSTANCE_ROLE}"
aws iam delete-role-policy --role-name "${INSTANCE_ROLE}" --policy-name eks-ai-ops-mcp 2>/dev/null || true
aws iam delete-role --role-name "${INSTANCE_ROLE}" 2>/dev/null || true

log "Deleting IAM role ${ACCESS_ROLE}"
aws iam detach-role-policy --role-name "${ACCESS_ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true
aws iam delete-role --role-name "${ACCESS_ROLE}" 2>/dev/null || true

log "Deleting ECR repository ${ECR_REPO}"
aws ecr delete-repository --repository-name "${ECR_REPO}" --region "${REGION}" --force 2>/dev/null || true

log "Deleting SSM parameters"
aws ssm delete-parameter --name "${SSM_URL_PARAM}" --region "${REGION}" 2>/dev/null || true
aws ssm delete-parameter --name "${SSM_KEY_PARAM}" --region "${REGION}" 2>/dev/null || true

log "Deleting App Runner CloudWatch log group"
aws logs delete-log-group --region "${REGION}" \
  --log-group-name "/aws/apprunner/${SERVICE}" 2>/dev/null || true

log "✅ MCP gateway teardown complete."
