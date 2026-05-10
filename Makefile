.PHONY: install test lint format build deploy clean help validate destroy destroy-stack destroy-data destroy-demo destroy-confirm

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
	sam build --template-file infrastructure/template.yaml --parallel

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

## ── Demo cleanup (AWS) ────────────────────────────────────────────────────
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
	  /eks-ai-ops-toolkit/eks-vpc-id \
	  /eks-ai-ops-toolkit/eks-private-subnet-1 \
	  /eks-ai-ops-toolkit/eks-private-subnet-2 2>/dev/null
	@echo "→ Deleting CloudWatch Lambda log groups..."
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/eks-ai-ops-toolkit 2>/dev/null
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/sre-slack-bot       2>/dev/null
	-aws logs delete-log-group --region $(REGION) --log-group-name /aws/lambda/sre-kubectl-helper  2>/dev/null

destroy-demo:  ## Delete throwaway demo CloudWatch alarms used by step-by-step.md
	-aws cloudwatch delete-alarms --region $(REGION) --alarm-names demo-eks-cpu-high 2>/dev/null

destroy: destroy-demo destroy-stack destroy-data  ## Nuke everything this project deployed (PROMPTS for confirmation)
	@echo ""
	@echo "✅ Teardown complete. Verify with:"
	@echo "    aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) 2>&1 | head -1"
	@echo "    aws dynamodb list-tables --region $(REGION) | grep -E 'sre-incidents|sre-deployments' || echo 'no demo tables'"
	@echo "    aws ssm get-parameters-by-path --path /eks-ai-ops-toolkit --region $(REGION) --query 'Parameters[].Name'"

destroy-confirm:  ## Same as 'destroy' but ASKS first (recommended)
	@read -p "Delete stack '$(STACK)' + DynamoDB + SSM + log groups in $(REGION)? [y/N] " yn && \
	  [ "$$yn" = "y" ] || [ "$$yn" = "Y" ] || (echo "Aborted." && exit 1)
	@$(MAKE) destroy STACK=$(STACK) REGION=$(REGION)

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
