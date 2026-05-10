"""
Shared pytest fixtures for the EKS SRE Agent test suite.

Fixture scopes:
  - function (default): fresh per test
  - module: shared within a test file
  - session: shared across the whole test run

AWS mocking strategy:
  - moto patches boto3 calls at the service level.
  - The @mock_aws decorator (moto >=5) replaces all individual service decorators.
  - Set AWS_DEFAULT_REGION and dummy credentials before importing any boto3 code.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest

# ---------------------------------------------------------------------------
# AWS credential stubs (must be set before any boto3 import in tests)
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# ---------------------------------------------------------------------------
# Application env stubs
# ---------------------------------------------------------------------------
os.environ.setdefault("CLUSTER_NAME", "test-cluster")
os.environ.setdefault("INCIDENT_TABLE", "sre-incidents-test")
os.environ.setdefault("DEPLOY_TABLE", "sre-deployments-test")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_CHANNEL", "C123TEST")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("GITHUB_TOKEN", "github_pat_test")
os.environ.setdefault("GITHUB_REPO", "testorg/test-repo")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("LLM_PROVIDER", "anthropic")


# ---------------------------------------------------------------------------
# Sample EventBridge events
# ---------------------------------------------------------------------------


@pytest.fixture()
def cloudwatch_alarm_event() -> dict[str, Any]:
    """Simulates a CloudWatch Alarm state change via EventBridge."""
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": "sre-prod-api-checkout-error-rate-high",
            "state": {
                "value": "ALARM",
                "reason": "Threshold crossed: 15.2% error rate",
            },
            "previousState": {"value": "OK"},
            "configuration": {"description": "Error rate for checkout service"},
        },
    }


@pytest.fixture()
def eks_audit_event() -> dict[str, Any]:
    """Simulates an EKS audit event (pod eviction)."""
    return {
        "source": "aws.eks",
        "detail-type": "AWS API Call via CloudTrail",
        "detail": {
            "verb": "delete",
            "resource": {"resource": "pods"},
            "requestParameters": {"namespace": "api", "name": "checkout-6f9b4c-xkpj2"},
            "responseElements": {"metadata": {"namespace": "api", "name": "checkout-6f9b4c-xkpj2"}},
            "user": {"username": "system:node:ip-10-0-1-5"},
            "errorCode": "Forbidden",
        },
    }


@pytest.fixture()
def scheduled_sweep_event() -> dict[str, Any]:
    """Simulates a scheduled health sweep event."""
    return {
        "source": "sre.scheduled",
        "detail": {
            "cluster": "prod",
            "check_name": "pod-crashloop-check",
            "namespace": "api",
            "resource_type": "pod",
            "resource_name": "checkout-6f9b4c-xkpj2",
            "findings": ["CrashLoopBackOff: 8 restarts in 10 minutes"],
        },
    }


@pytest.fixture()
def unknown_source_event() -> dict[str, Any]:
    """An event with an unknown source (tests the fallback path)."""
    return {
        "source": "custom.internal",
        "detail": {"alarmName": "my-custom-alarm"},
    }


# ---------------------------------------------------------------------------
# Sample normalised incident dicts
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_incident() -> dict[str, Any]:
    return {
        "source": "cloudwatch_alarm",
        "cluster_name": "prod",
        "alarm_name": "sre-prod-api-checkout-error-rate-high",
        "alarm_description": "Error rate for checkout service",
        "previous_state": "OK",
        "current_state": "ALARM",
        "state_reason": "Threshold crossed: 15.2% error rate",
        "namespace": "api",
        "resource_type": "deployment",
        "resource_name": "checkout",
        "related_alarms": [],
        "raw_event": {},
    }


@pytest.fixture()
def sample_analysis() -> dict[str, Any]:
    return {
        "root_cause": "High error rate caused by OOMKill in checkout pod",
        "severity": "high",
        "fix_type": "auto",
        "fix_description": "Increase memory limit from 256Mi to 512Mi",
        "pr_files": [
            {
                "path": "k8s/checkout/deployment.yaml",
                "content": "apiVersion: apps/v1\nkind: Deployment\n...",
                "description": "Increase memory limit",
            }
        ],
        "runbook_steps": [
            "Check pod OOMKill events: kubectl describe pod -n api -l app=checkout",
            "Review memory usage metrics in CloudWatch Container Insights",
            "Apply memory limit increase via auto-fix PR",
        ],
    }


@pytest.fixture()
def sample_analysis_manual() -> dict[str, Any]:
    return {
        "root_cause": "Database connection pool exhausted",
        "severity": "critical",
        "fix_type": "manual",
        "fix_description": "Restart the database connection pool and check for connection leaks",
        "pr_files": [],
        "runbook_steps": [
            "Check DB connection count: kubectl exec -n api deploy/checkout -- nc -zv postgres 5432",
            "Restart the pod: kubectl rollout restart deploy/checkout -n api",
        ],
    }


# ---------------------------------------------------------------------------
# Mock LLM client
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_llm_client() -> MagicMock:
    """A MagicMock LLM client that returns a final JSON answer on the first call."""
    from sre_agent.llm_client import ContentBlock, MessageResponse

    client = MagicMock()
    client.model_id = "mock/test-model"

    def _create_message(**kwargs: Any) -> MessageResponse:  # type: ignore[return]
        return MessageResponse(
            content=[
                ContentBlock(
                    type="text",
                    text=json.dumps(
                        {
                            "root_cause": "Mock root cause",
                            "severity": "high",
                            "fix_type": "auto",
                            "fix_description": "Mock fix",
                            "pr_files": [
                                {
                                    "path": "k8s/fix.yaml",
                                    "content": "# fix",
                                    "description": "mock file",
                                }
                            ],
                            "runbook_steps": ["Step 1", "Step 2"],
                        }
                    ),
                )
            ],
            stop_reason="end_turn",
            model="mock",
            usage_input_tokens=100,
            usage_output_tokens=200,
        )

    client.create_message.side_effect = _create_message
    return client


@pytest.fixture()
def mock_llm_with_tool_call(mock_llm_client: MagicMock) -> MagicMock:
    """LLM client that makes one tool call then returns a final answer."""
    from sre_agent.llm_client import ContentBlock, MessageResponse

    call_count = 0

    def _create_message(**kwargs: Any) -> MessageResponse:  # type: ignore[return]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: request a tool
            return MessageResponse(
                content=[
                    ContentBlock(
                        type="tool_use",
                        tool_use_id="tu_test_001",
                        name="list_related_alerts",
                        input={"service_name": "checkout"},
                    )
                ],
                stop_reason="tool_use",
                model="mock",
            )
        # Second call: final answer
        return MessageResponse(
            content=[
                ContentBlock(
                    type="text",
                    text=json.dumps(
                        {
                            "root_cause": "High error rate, no related alarms found",
                            "severity": "medium",
                            "fix_type": "manual",
                            "fix_description": "Investigate manually",
                            "pr_files": [],
                            "runbook_steps": ["Check logs"],
                        }
                    ),
                )
            ],
            stop_reason="end_turn",
            model="mock",
        )

    mock_llm_client.create_message.side_effect = _create_message
    return mock_llm_client


# ---------------------------------------------------------------------------
# DynamoDB table fixture (requires moto @mock_aws decorator on the test)
# ---------------------------------------------------------------------------


@pytest.fixture()
def dynamodb_tables() -> None:
    """Create the DynamoDB tables used by the application."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")

    # Incident table
    ddb.create_table(
        TableName="sre-incidents-test",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
    )

    # Deployments table
    ddb.create_table(
        TableName="sre-deployments-test",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "service_name", "AttributeType": "S"},
            {"AttributeName": "deployed_at", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "service_name", "KeyType": "HASH"},
            {"AttributeName": "deployed_at", "KeyType": "RANGE"},
        ],
    )


# ---------------------------------------------------------------------------
# Slack API mock helper
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_slack_post() -> Any:
    """Patches SlackClient._post to avoid real HTTP calls."""
    with patch(
        "sre_agent.slack_client.SlackClient._post",
        return_value={"ok": True, "ts": "1234567890.123456"},
    ) as mock:
        yield mock


@pytest.fixture()
def mock_github_request() -> Any:
    """Patches GitHubClient._request to avoid real GitHub API calls."""
    import urllib.error

    def _fake_request(method: str, path: str, payload: dict | None = None) -> Any:
        if "/git/ref/" in path:
            return {"object": {"sha": "abc123def456"}}
        if "/git/refs" in path:
            return {}
        if "/contents/" in path and method == "GET":
            raise urllib.error.HTTPError(url=path, code=404, msg="Not Found", hdrs={}, fp=None)  # type: ignore[arg-type]
        if "/contents/" in path and method == "PUT":
            return {"content": {"sha": "newsha"}}
        if "/pulls" in path:
            return {"html_url": "https://github.com/testorg/test-repo/pull/42"}
        return {}

    with patch(
        "sre_agent.github_client.GitHubClient._request",
        side_effect=_fake_request,
    ) as mock:
        yield mock
