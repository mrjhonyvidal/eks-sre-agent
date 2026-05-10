# Tests — EKS SRE Agent

## Overview

The test suite uses **pytest** with **moto** for AWS service mocking.
Coverage target is **90%** (enforced in CI).

```
tests/
├── conftest.py          # Shared fixtures — events, incidents, mock LLM, DynamoDB tables
├── test_enricher.py     # Event normalisation (CloudWatch, EKS audit, scheduled)
├── test_llm_client.py   # Multi-LLM abstraction (Anthropic + Bedrock)
├── test_agent.py        # SREAgent — agentic loop, tool dispatch, tool implementations
├── test_handler.py      # Lambda handler — full flow, dedup, PR creation conditions
├── test_slack_client.py # Slack Block Kit client
├── test_github_client.py # GitHub PR creator — branch, commits, PR
├── test_bot_handler.py  # Slack bot compatibility entrypoint tests
├── test_orchestrator.py # K8S orchestrator intent classify + route/exit
└── test_integration.py  # End-to-end with moto-mocked DynamoDB
```

---

## Quick start

```bash
# Install all dev dependencies (once)
pip install -e ".[dev]"

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a single file
pytest tests/test_agent.py

# Run a single test
pytest tests/test_agent.py::TestSREAgentAnalyze::test_returns_parsed_json_on_first_response

# Run without coverage (faster)
pytest --no-cov

# Run only integration tests
pytest tests/test_integration.py

# Run with coverage HTML report
pytest --cov-report=html
open htmlcov/index.html
```

---

## Test categories

### Unit tests
Mock all external services. Fast, no network calls.

```bash
pytest tests/test_enricher.py tests/test_agent.py tests/test_handler.py \
       tests/test_slack_client.py tests/test_github_client.py tests/test_bot_handler.py \
       tests/test_orchestrator.py tests/test_llm_client.py
```

### Integration tests
Use moto to simulate DynamoDB. Validate full Lambda flows.

```bash
pytest tests/test_integration.py
```

---

## Coverage reporting

```bash
# Terminal report (default)
pytest

# Generate HTML report
pytest --cov-report=html
open htmlcov/index.html

# Generate XML (for CI)
pytest --cov-report=xml:coverage.xml
```

Coverage configuration lives in `pyproject.toml`:
- Minimum: **90%** (line + branch)
- Excludes: `tests/`, `conftest.py`, `kubectl_helper/`

---

## Mocking patterns

### Mock the LLM client

Use the `mock_llm_client` fixture from `conftest.py`:

```python
def test_my_thing(mock_llm_client, sample_incident):
    from sre_agent.agent import SREAgent
    with patch("sre_agent.agent.boto3"):
        agent = SREAgent(llm_client=mock_llm_client)
        result = agent.analyze(sample_incident)
    assert result["severity"] == "high"
```

To simulate a tool call before the final answer, use `mock_llm_with_tool_call`.

### Mock AWS services with moto

```python
from moto import mock_aws

@mock_aws
def test_dynamodb_write():
    import boto3
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(...)
    # your test
```

### Mock Slack HTTP

```python
def test_slack(mock_slack_post):
    # mock_slack_post patches SlackClient._post
    # Returns {"ok": True, "ts": "1234567890.123456"} by default
    slack = SlackClient()
    ts = slack.post_investigating(incident)
    assert ts == "1234567890.123456"
```

### Mock GitHub API

```python
def test_github(mock_github_request):
    # mock_github_request patches GitHubClient._request
    # Simulates branch SHA, file 404, file PUT, and PR creation
    gh = GitHubClient()
    pr_url = gh.create_fix_pr("id1", analysis, incident)
    assert "pull/42" in pr_url
```

---

## Adding new tests

### New tool test

1. Add your tool to `TOOLS` in `agent.py` and implement `_tool_<name>`
2. Add to `test_agent.py::TestToolImplementations`:

```python
def test_my_new_tool(self, mock_llm_client):
    with patch("sre_agent.agent.boto3"):
        agent = SREAgent(llm_client=mock_llm_client)
        result = agent._tool_my_new_tool(param="value")
    assert "expected_key" in result
```

### New enricher source

Add to `test_enricher.py`:

```python
def test_my_new_source(self):
    raw = {"source": "my.source", "detail": {...}}
    result = enrich_event(raw)
    assert result["source"] == "my_source"
```

---

## CI integration

Tests run automatically on every push and PR via GitHub Actions.

See `.github/workflows/ci.yml` for the full pipeline:
- Linting with Ruff
- Tests on Python 3.11 and 3.12
- Coverage check (≥90%)
- Coverage comment on PRs
- SAM build validation

### Running locally with the same env as CI

```bash
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=testing
export AWS_SECRET_ACCESS_KEY=testing
export ANTHROPIC_API_KEY=sk-ant-test
export LLM_PROVIDER=anthropic
export CLUSTER_NAME=test-cluster
export INCIDENT_TABLE=sre-incidents-test
export DEPLOY_TABLE=sre-deployments-test
export SLACK_BOT_TOKEN=xoxb-test
export SLACK_CHANNEL=C123TEST
export SLACK_SIGNING_SECRET=test-secret
export GITHUB_TOKEN=github_pat_test
export GITHUB_REPO=testorg/test-repo
pytest
```

---

## Pre-commit hooks (optional)

Install pre-commit to run linting before every commit:

```bash
pip install pre-commit
pre-commit install
```

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```
