.PHONY: install test lint format build deploy clean help validate destroy destroy-stack destroy-data destroy-trigger destroy-confirm destroy-all destroy-eks verify-cleanup

## ── Install ─────────────────────────────────────────────────────────────────────
install:  ## Install all dependencies (runtime + dev tools)
	pip install -e ".[dev]"

## ── Test ────────────────────────────────────────────────────────────────────────
test:  ## Run all tests with coverage (≥80% required)
	pytest

test-fast:  ## Run tests without coverage reporting
	pytest --no-cov -q

test-unit:  ## Run only unit tests (no integration)
	pytest tests/ --ignore=tests/test_integration.py --no-cov -v

test-integration:  ## Run only integration tests
	pytest tests/test_integration.py -v

test-watch:  ## Re-run tests on file changes (requires pytest-watch)
	ptw -- --no-cov -q

## ── Lint & Format ────────────────────────────────────────────────────────
lint:  ## Ruff lint check
	ruff check .

format:  ## Auto-format code with Ruff
	ruff format .

format-check:  ## Check formatting without making changes
	ruff format --check .

typecheck:  ## mypy type checking (informational)
	mypy src/eks_ai_ops/ || true

check: lint format-check typecheck  ## Run all code quality checks

## ── Build & Deploy ───────────────────────────────────────────────────────
validate:  ## SAM template + Python lint sanity check
	sam validate --lint --template-file infrastructure/template.yaml

build:  ## SAM build (validates template + packages Lambda code)
	sam build --template-file infrastructure/template.yaml --base-dir . --parallel

deploy: build  ## SAM guided deploy (prompts for parameters)
	sam deploy --guided \
	  --template-file infrastructure/template.yaml \
	  --stack-name eks-ai-ops-toolkit \
	  --capabilities CAPABILITY_IAM

deploy-fast: build  ## SAM deploy without re-prompting (uses samconfig.toml)
	sam deploy \
	  --template-file infrastructure/template.yaml \
	  --stack-name eks-ai-ops-toolkit \
	  --capabilities CAPABILITY_IAM \
	  --no-confirm-changeset \
	  --no-fail-on-empty-changeset

logs-agent:  ## Tail proactive SRE Agent Lambda logs
	aws logs tail /aws/lambda/eks-ai-ops-toolkit --follow

logs-bot:  ## Tail Slack Bot Lambda logs
	aws logs tail /aws/lambda/sre-slack-bot --follow

## ── Utilities ─────────────────────────────────────────────────────────────
clean:  ## Remove build artifacts and caches
	rm -rf .aws-sam/ htmlcov/ .coverage coverage.xml .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

## ── Teardown (AWS) ─────────────────────────────────────────────────────
# Stack name + region must match `make deploy`. Override on the CLI if needed:
#   make destroy STACK=my-stack REGION=eu-west-1
STACK  ?= eks-ai-ops-toolkit
REGION ?= $(shell aws configure get region 2>/dev/null || echo us-east-1)

destroy-stack:  ## Delete the CloudFormation stack (Lambda, API Gateway, EventBridge, SG)
	@echo "→ Deleting CloudFormation stack '$(STACK)' in $(REGION)..."
	sam delete --stack-name $(STACK) --region $(REGION) --no-prompts || true

destroy-data:  ## Delete retained DynamoDB tables, SSM params, and Lambda log groups
	@echo "→ Deleting DynamoDB tables (DeletionPolicy: Retain)..."
	-aws dynamodb delete-table --region $(REGION) --table-name sre-incidents   --no-cli-pager 2>/dev/null
	-aws dynamodb delete-table --region $(REGION) --table-name sre-deployments --no-cli-pager 2>/dev/null
	@echo "→ Deleting SSM parameters under /eks-ai-ops-toolkit/*..."
	-aws ssm delete-parameters --region $(REGION) --no-cli-pager --names \
	  /eks-ai-ops-toolkit/slack-bot-token \
	  /eks-ai-ops-toolkit/slack-signing-secret \
	  /eks-ai-ops-toolkit/github-token \
	  /eks-ai-ops-toolkit/anthropic-api-key \
	  /eks-ai-ops-toolkit/mcp-gateway-url \
	  /eks-ai-ops-toolkit/mcp-gateway-api-key \
	  /eks-ai-ops-toolkit/eks-vpc-id \
	  /eks-ai-ops-toolkit/eks-private-subnet-1 \
	  /eks-ai-ops-toolkit/eks-private-subnet-2 2>/dev/null
	@echo "→ Deleting CloudWatch Lambda log groups..."
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/eks-ai-ops-toolkit 2>/dev/null
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/sre-slack-bot       2>/dev/null
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/sre-kubectl-helper  2>/dev/null

destroy-trigger:  ## Delete the throwaway CloudWatch alarm used by Scenario A
	-aws cloudwatch delete-alarms --region $(REGION) --alarm-names eks-ai-ops-trigger 2>/dev/null

destroy: destroy-trigger destroy-stack destroy-data  ## Nuke everything this project deployed (PROMPTS for confirmation)
	@echo ""
	@echo "✅ Teardown complete. Verify with:"
	@echo "    aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) 2>&1 | head -1"
	@echo "    aws dynamodb list-tables --region $(REGION) | grep -E 'sre-incidents|sre-deployments' || echo 'no toolkit tables'"
	@echo "    aws ssm get-parameters-by-path --path /eks-ai-ops-toolkit --region $(REGION) --query 'Parameters[].Name'"

destroy-confirm:  ## Same as 'destroy' but ASKS first (recommended)
	@read -p "Delete stack '$(STACK)' + DynamoDB + SSM + log groups in $(REGION)? [y/N] " yn && \
	  [ "$$yn" = "y" ] || [ "$$yn" = "Y" ] || (echo "Aborted." && exit 1)
	@$(MAKE) destroy STACK=$(STACK) REGION=$(REGION)

destroy-eks:  ## Delete EKS cluster and nodegroup stacks (eksctl-managed)
	@echo "→ Disabling termination protection on EKS stacks..."
	-aws cloudformation update-termination-protection --no-enable-termination-protection \
	  --stack-name eksctl-eks-ai-ops-nodegroup-ng-632887e6 --region $(REGION) 2>/dev/null
	-aws cloudformation update-termination-protection --no-enable-termination-protection \
	  --stack-name eksctl-eks-ai-ops-cluster --region $(REGION) 2>/dev/null
	@echo "→ Deleting EKS stacks (this may take 10+ minutes)..."
	-aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-nodegroup-ng-632887e6 --region $(REGION)
	-aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-cluster --region $(REGION)

destroy-ec2:  ## Terminate all EC2 instances (related to this project)
	@echo "→ Terminating EC2 instances..."
	-aws ec2 describe-instances --query 'Reservations[].Instances[?State.Name==`running`].InstanceId' --region $(REGION) | \
	  xargs -I {} aws ec2 terminate-instances --instance-ids {} --region $(REGION) 2>/dev/null

destroy-all: destroy-confirm destroy-eks destroy-ec2  ## ⚠️ NUCLEAR: Delete everything (toolkit + EKS + EC2 + data)
	@echo ""
	@echo "✅ Full teardown initiated. Stacks may take 10+ minutes to delete."
	@echo "Monitor progress with: make verify-cleanup REGION=$(REGION)"
	@echo ""
	@echo "See CLEANUP.md for detailed information and troubleshooting."

verify-cleanup:  ## Verify all resources have been deleted
	@echo "=== CloudFormation Stacks ==="
	@aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --region $(REGION) --query 'StackSummaries[].[StackName,StackStatus]'
	@echo ""
	@echo "=== Running EC2 Instances ==="
	@aws ec2 describe-instances --query 'Reservations[].Instances[?State.Name==`running`].[InstanceId,InstanceType]' --region $(REGION)
	@echo ""
	@echo "=== Lambda Functions ==="
	@aws lambda list-functions --region $(REGION) --query 'Functions[].FunctionName'
	@echo ""
	@echo "=== DynamoDB Tables ==="
	@aws dynamodb list-tables --region $(REGION) --query 'TableNames[]'
	@echo ""
	@echo "=== S3 Buckets ==="
	@aws s3 ls
	@echo ""
	@echo "If all lists are empty, cleanup is complete!"

coverage-html:  ## Open HTML coverage report
	pytest --cov-report=html --no-cov-on-fail
	open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html

setup-ssm:  ## Interactive SSM parameter setup (requires AWS profile)
	@echo "Setting up SSM parameters..."
	@read -p "Slack Bot Token (xoxb-...): " SLACK_TOKEN && \
	  aws ssm put-parameter --name /eks-ai-ops-toolkit/slack-bot-token --value "$$SLACK_TOKEN" --type SecureString --overwrite
	@read -p "Slack Signing Secret: " SLACK_SECRET && \
	  aws ssm put-parameter --name /eks-ai-ops-toolkit/slack-signing-secret --value "$$SLACK_SECRET" --type SecureString --overwrite
	@read -p "GitHub Token (github_pat_...): " GH_TOKEN && \
	  aws ssm put-parameter --name /eks-ai-ops-toolkit/github-token --value "$$GH_TOKEN" --type SecureString --overwrite
	@read -p "Anthropic API Key (sk-ant-..., press Enter to skip for Bedrock): " ANTHROPIC_KEY && \
	  [ -n "$$ANTHROPIC_KEY" ] && aws ssm put-parameter --name /eks-ai-ops-toolkit/anthropic-api-key --value "$$ANTHROPIC_KEY" --type SecureString --overwrite || echo "Skipped Anthropic key"
	@echo "✅ SSM parameters set. Run 'make deploy' next."

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
