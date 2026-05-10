# CLAUDE.md — Context for Claude and AI coding assistants

This file provides context to help Claude (and any AI coding assistant) understand
this repository quickly and contribute effectively.

---

## What this project is

**EKS SRE Agent** is a serverless, AI-powered Site Reliability Engineering agent for AWS.

It:
1. **Detects** incidents from CloudWatch Alarms, EKS audit events, and scheduled sweeps via EventBridge
2. **Investigates** using an agentic tool-call loop (fetching pod logs, k8s resource descriptions, CloudWatch metrics, deployment history)
3. **Posts** a structured Slack alert with root cause + runbook
4. **Raises a GitHub PR** with auto-generated YAML fixes for safe, high-severity incidents
5. **Chats** with on-call engineers via a Slack bot backed by the same LLM

---

## Architecture overview

```
CloudWatch Alarms ─┐
EKS Audit Events  ─┤──► EventBridge ──► sre_agent/handler.py (SRE Agent Lambda)
Scheduled Sweep   ─┘                         │
                                       ┌──────┴──────┐
                                       ▼             ▼
                                   Slack alert   GitHub PR
                                   + DynamoDB       │
                                       │        (auto-fix branch)
                                   user reply
                                       ▼
                                  sre_agent/bot_handler.py (Bot Lambda)
```

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| Two Lambdas | Agent runs up to 5 min; bot must respond in <3s |
| EventBridge as bus | Decouples sources; add new monitors without touching agent |
| DynamoDB for state | Dedup + bot conversation context |
| Tool-use loop (not MCP) | No sidecar infra needed |
| LLM abstraction layer | Supports Anthropic and Bedrock interchangeably |
| `sre/auto-fix-{id}` branch | PRs always require human review before merge |

---

## Multi-LLM support

The agent supports two LLM backends selected via `LLM_PROVIDER` env var:

| Provider | Value | Auth method |
|----------|-------|------------|
| Anthropic Claude | `anthropic` (default) | `ANTHROPIC_API_KEY` |
| Amazon Bedrock | `bedrock` | IAM role (no extra key) |

**Bedrock is the recommended AWS-native choice** — no extra API key, uses your Lambda
execution role, and supports Claude + AWS-native models (Nova, Titan).

Model selection:
- `ANTHROPIC_MODEL` — e.g. `claude-sonnet-4-20250514`
- `BEDROCK_MODEL_ID` — e.g. `us.anthropic.claude-sonnet-4-5-20250514-v1:0`

---

## Code organisation

```
eks-sre-agent/
├── sre_agent/           # Production package
│   ├── __init__.py
│   ├── llm_client.py    # Multi-LLM abstraction (Anthropic + Bedrock)
│   ├── agent.py         # SREAgent — agentic RCA loop
│   ├── handler.py       # Lambda 1 — EventBridge → agent → Slack + GitHub
│   ├── bot_handler.py   # Lambda 2 — Slack Events API → LLM chat
│   ├── enricher.py      # EventBridge payload normaliser
│   ├── slack_client.py  # Slack Block Kit client (no Bolt SDK)
│   └── github_client.py # GitHub PR creator (native urllib, no PyGithub)
├── kubectl_helper/      # Lambda 3 — kubectl inside EKS VPC
│   └── handler.py
├── infrastructure/
│   └── template.yaml    # AWS SAM template
├── tests/
│   ├── conftest.py      # Shared fixtures (events, incidents, mock LLM)
│   ├── test_enricher.py
│   ├── test_llm_client.py
│   ├── test_agent.py
│   ├── test_handler.py
│   ├── test_slack_client.py
│   ├── test_github_client.py
│   ├── test_bot_handler.py
│   └── test_integration.py
├── pyproject.toml       # Build, Ruff, mypy, pytest config (90% coverage)
└── requirements.txt     # Runtime deps (anthropic + boto3 only)
```

---

## Common tasks for Claude

### Run tests
```bash
pip install -e ".[dev]"
pytest
```

### Run linting
```bash
ruff check .
ruff format --check .
```

### Add a new tool to the agent

1. Add a new entry to `TOOLS` list in `sre_agent/agent.py`
2. Implement `_tool_<name>` method on `SREAgent`
3. Register it in `_dispatch_tool` handlers dict
4. Write tests in `tests/test_agent.py`

### Add a new EventBridge source

1. Add a `_from_<source>` function in `sre_agent/enricher.py`
2. Add the source to the dispatcher in `enrich_event()`
3. Write tests in `tests/test_enricher.py`

### Switch to Bedrock

```bash
export LLM_PROVIDER=bedrock
export BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250514-v1:0
# No ANTHROPIC_API_KEY needed — uses Lambda IAM role
```

Or in the SAM template, set the `LLM_PROVIDER` environment variable.

### Deploy with one command

After setting up SSM parameters (see README.md):
```bash
sam build && sam deploy --guided \
  --parameter-overrides \
    ClusterName=my-eks-cluster \
    SlackChannel="#sre-alerts" \
    GitHubRepo="myorg/k8s-infra"
```

---

## Environment variables reference

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `LLM_PROVIDER` | No | `anthropic` | LLM backend: `anthropic` or `bedrock` |
| `ANTHROPIC_API_KEY` | When `LLM_PROVIDER=anthropic` | — | Anthropic API key |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-4-20250514` | Anthropic model ID |
| `BEDROCK_MODEL_ID` | When `LLM_PROVIDER=bedrock` | `us.anthropic.claude-sonnet-4-5-20250514-v1:0` | Bedrock model ID |
| `CLUSTER_NAME` | Yes | `eks-cluster` | EKS cluster to monitor |
| `SLACK_BOT_TOKEN` | Yes | — | Slack bot OAuth token |
| `SLACK_CHANNEL` | Yes | — | Slack channel ID or name |
| `SLACK_SIGNING_SECRET` | Yes (bot Lambda) | — | Slack request verification |
| `GITHUB_TOKEN` | Yes | — | GitHub PAT or fine-grained token |
| `GITHUB_REPO` | Yes | — | `org/repo` for auto-fix PRs |
| `GITHUB_BASE_BRANCH` | No | `main` | Base branch for PRs |
| `INCIDENT_TABLE` | No | `sre-incidents` | DynamoDB incident table name |
| `DEPLOY_TABLE` | No | `sre-deployments` | DynamoDB deployments table name |
| `KUBECTL_LAMBDA` | No | `sre-kubectl-helper` | kubectl helper Lambda name |

---

## Testing conventions

- **Unit tests**: mock all external deps (boto3, Anthropic, Slack, GitHub)
- **Integration tests**: use moto to mock AWS services at the SDK level
- **Coverage**: 90% minimum, enforced by `pytest --cov-fail-under=90`
- **Fixtures**: defined in `tests/conftest.py`, use pytest fixtures with function scope
- **Mocking pattern**: prefer `patch()` context managers over class-level decorators for clarity

---

## Cost optimisation (built in)

- Lambda ARM64 (Graviton) for ~20% cost reduction vs x86
- DynamoDB on-demand billing — pay per request
- DynamoDB TTL — auto-expire incidents after 7 days
- boto3 client reuse across warm invocations (module-level singletons)
- Claude Haiku / Nova Lite available for lighter analysis tasks (set `BEDROCK_MODEL_ID`)
- RDS is intentionally not used — DynamoDB handles all state

---

## Conventions

- Python 3.11+ with `from __future__ import annotations`
- Type annotations on all public functions
- Structured logging: `logger.info("message key=val key=val")` (key=value pairs)
- No secrets in code — all via SSM Parameter Store / Lambda env vars
- Ruff for linting + formatting (line length 100)
- All imports at top of file; `boto3.client()` inside methods only when needed per-call
