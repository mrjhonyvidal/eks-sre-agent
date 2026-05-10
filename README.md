# EKS SRE Agent 🤖

[![CI](https://github.com/mrjhonyvidal/eks-sre-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mrjhonyvidal/eks-sre-agent/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/mrjhonyvidal/eks-sre-agent/branch/main/graph/badge.svg)](https://codecov.io/gh/mrjhonyvidal/eks-sre-agent)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A serverless, AI-powered SRE agent for AWS EKS. It monitors your cluster, performs root cause analysis using Claude or Amazon Bedrock, raises GitHub PRs for auto-fixable issues, and lets your team chat with it via Slack.

---

## Architecture

```
CloudWatch Alarms ─┐
EKS Audit Events  ─┤──► EventBridge ──► SRE Agent Lambda (LLM)
Scheduled Sweep   ─┘                         │
                                       ┌──────┴──────┐
                                       ▼             ▼
                                   Slack alert   GitHub PR
                                   + DynamoDB       │
                                       │        (auto-fix branch)
                                   user reply
                                       ▼
                                  Slack Bot Lambda (LLM)
```

---

## LLM backends

| Backend | Env var | Auth | Best for |
|---------|---------|------|----------|
| **Amazon Bedrock** ⭐ | `LLM_PROVIDER=bedrock` | IAM role | AWS-native; no extra API key; single bill |
| Anthropic Claude | `LLM_PROVIDER=anthropic` | `ANTHROPIC_API_KEY` | Direct API access |

**Bedrock is the recommended choice for AWS deployments.**
It uses your Lambda IAM execution role — no additional secrets to manage.

Supported Bedrock models (set via `BEDROCK_MODEL_ID`):

| Model | Cost | Latency | Use case |
|-------|------|---------|----------|
| `us.amazon.nova-lite-v1:0` | $ | Fast | Default — highly cost-effective and capable |
| `us.anthropic.claude-3-5-haiku-20241022-v1:0` | $ | Fast | Fast Anthropic alternative |
| `us.amazon.nova-pro-v1:0` | $$ | Medium | Better reasoning for complex issues |
| `us.anthropic.claude-sonnet-4-5-20250514-v1:0` | $$$ | Medium | Premium model for deepest analysis |

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| Two separate Lambdas | Agent (up to 5 min) and bot (must respond <3s) have very different latency needs |
| EventBridge as the bus | Decouples sources — add new monitors without touching the agent |
| DynamoDB for incident state | Gives the bot conversation context; dedup prevents alert storms |
| Tool-use loop (not MCP) | Simpler deployment — no sidecar infrastructure |
| Branch `sre/auto-fix-{id}` | PRs are never merged automatically — always require human review |
| RDS not used | DynamoDB handles all state; no extra cost or operational burden |

---

## Cost estimate (us-east-1, ~50 incidents/day)

| Component | Est. monthly cost |
|-----------|-------------------|
| Lambda (agent, ~30s avg, ARM64) | ~$1.20 |
| Lambda (bot, ~2s avg, ARM64) | ~$0.15 |
| DynamoDB (on-demand) | ~$0.50 |
| Bedrock (Nova Lite, ~2k tokens/incident) | ~$1.50 |
| **Total (Nova Lite)** | **~$3/month** |

> Note: To use the premium Claude Sonnet model, expect costs around ~$17/month. Nova Lite is the recommended default for an excellent balance of cost and capability.

---

## Quick start — one command after envs are set

### 1. Store secrets in SSM Parameter Store

We provide a helpful Makefile target to set this up quickly. You will need:
- Slack Bot Token and Signing Secret
- GitHub PAT

```bash
make setup-ssm
```

### 2. Deploy (one command)

```bash
# Install SAM CLI (once) if you haven't already
brew install aws-sam-cli

# Deploy
make deploy
```

After deploy, copy the `SlackBotEndpoint` from the stack outputs.

### 3. Configure Slack app

1. Create a new app at https://api.slack.com/apps
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `app_mentions:read`, `channels:history`
3. **Event Subscriptions** → Enable → paste the `SlackBotEndpoint` URL
4. Subscribe to bot events: `app_mention`
5. **Interactivity & Shortcuts** → Enable → same URL
6. Install the app to your workspace, invite `@SREBot` to `#sre-alerts`

---

## Deploy via GitHub Actions (CI/CD)

Set these GitHub repository variables and secrets, then push to `main`:

**Variables** (non-sensitive, set under Settings → Variables):
```
EKS_CLUSTER_NAME = my-eks-cluster
AWS_REGION       = us-east-1
SLACK_CHANNEL    = #sre-alerts
GITHUB_REPO      = myorg/k8s-infra
```

**Secrets** (sensitive):
```
AWS_DEPLOY_ROLE_ARN = arn:aws:iam::123456789012:role/github-deploy-role
```

The deploy workflow (`.github/workflows/deploy.yml`) will:
1. Authenticate to AWS via OIDC (no static AWS keys)
2. Pre-fill SAM parameters from GitHub variables
3. Build and deploy automatically on every push to `main`

You can also trigger manually with custom values via **Actions → Deploy to AWS → Run workflow**.

---

## Local development & testing

### Setup

```bash
# Install all dependencies (including dev tools)
pip install -e ".[dev]"
```

### Run tests

```bash
# All tests with coverage (must be ≥90%)
pytest

# Specific module
pytest tests/test_agent.py -v

# Without coverage (faster iteration)
pytest --no-cov
```

### Run linting

```bash
ruff check .          # lint
ruff format --check . # format check
ruff format .         # auto-format
```

### Local invoke

```bash
export LLM_PROVIDER=anthropic  # or bedrock
export ANTHROPIC_API_KEY=sk-ant-...
export CLUSTER_NAME=local-test
export INCIDENT_TABLE=sre-incidents-dev
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL=C123ABC
export GITHUB_TOKEN=github_pat_...
export GITHUB_REPO=myorg/infra

python -c "
from sre_agent.handler import handler
event = {
    'source': 'aws.cloudwatch',
    'detail': {
        'alarmName': 'sre-prod-api-checkout-error-rate-high',
        'state': {'value': 'ALARM', 'reason': 'Threshold crossed: 15.2% error rate'},
        'previousState': {'value': 'OK'},
        'configuration': {},
    }
}
print(handler(event, None))
"
```

---

## CloudWatch alarm naming convention

Name your alarms using this pattern so the enricher can parse them:

```
sre-{cluster}-{namespace}-{service}-{metric}
# Examples:
sre-prod-api-checkout-error-rate-high
sre-prod-data-postgres-cpu-critical
```

Or send a custom EventBridge event:

```json
{
  "source": "sre.scheduled",
  "detail": {
    "cluster": "prod",
    "check_name": "pod-crashloop-check",
    "namespace": "api",
    "resource_type": "pod",
    "resource_name": "checkout-6f9b4c-xkpj2",
    "findings": ["CrashLoopBackOff: 8 restarts in 10 minutes"]
  }
}
```

---

## Register deployments (optional but recommended)

Add a CI/CD step to write to DynamoDB so the agent can correlate incidents with recent deploys:

```python
import boto3, time
from datetime import datetime

boto3.resource("dynamodb").Table("sre-deployments").put_item(Item={
    "service_name": f"{cluster}/{namespace}/{service}",
    "deployed_at": datetime.utcnow().isoformat(),
    "ttl": int(time.time()) + 30 * 86400,
    "image": image_tag,
    "deployed_by": git_actor,
    "commit_sha": git_sha,
})
```

---

## Using the Slack bot

Once an incident fires, the bot posts a message with:
- Root cause + severity
- Runbook checklist
- Link to the auto-fix PR (if generated)
- Buttons: **Ask agent**, **Resolve**, **False positive**

Chat with the agent in the thread:

```
@SREBot what's causing the OOMKill in the checkout pod?
@SREBot show me the exact kubectl commands to roll back
@SREBot was there a recent deploy that could have caused this?
```

---

## Extending the agent

### Add a new tool

In `sre_agent/agent.py`, add to `TOOLS` and implement `_tool_<name>`:

```python
{
    "name": "get_pagerduty_oncall",
    "description": "Returns the current on-call engineer for a service.",
    "input_schema": {
        "type": "object",
        "properties": {"service_name": {"type": "string"}},
        "required": ["service_name"],
    },
}
```

### Add a new event source

In `sre_agent/enricher.py`, add a `_from_*` function and register it in `enrich_event()`.

### Switch LLM model

```bash
# Use cheapest Bedrock model for cost optimisation
export LLM_PROVIDER=bedrock
export BEDROCK_MODEL_ID=us.amazon.nova-lite-v1:0

# Or use Claude Haiku for fast responses
export BEDROCK_MODEL_ID=us.anthropic.claude-3-5-haiku-20241022-v1:0
```

---

## License

MIT — see [LICENSE](LICENSE).
