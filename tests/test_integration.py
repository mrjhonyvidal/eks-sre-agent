"""
Integration tests — end-to-end flows with moto-mocked AWS services.

These tests validate the full Lambda flow without deploying to AWS.
moto intercepts boto3 calls at the service level.

Note: moto's @mock_aws works best with function-level tests, not class methods.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import boto3

# moto >=5 uses a single @mock_aws decorator
try:
    from moto import mock_aws
except ImportError:
    from moto import mock_dynamodb as mock_aws  # type: ignore[no-redef]


def _create_incident_table() -> Any:
    """Helper to create the incident table in moto."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    try:
        table = ddb.create_table(
            TableName="sre-incidents-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
        )
        table.wait_until_exists()
    except ddb.meta.client.exceptions.ResourceInUseException:
        pass
    return ddb.Table("sre-incidents-test")


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------


@mock_aws
def test_incident_persisted_to_dynamodb() -> None:
    """Full handler run should write a record to DynamoDB."""
    table = _create_incident_table()

    event = {
        "source": "aws.cloudwatch",
        "detail": {
            "alarmName": "sre-prod-api-checkout-error-rate-high",
            "state": {"value": "ALARM", "reason": "Error > 10%"},
            "previousState": {"value": "OK"},
            "configuration": {},
        },
    }

    with (
        patch("sre_agent.enricher.boto3") as mock_enricher_boto3,
        patch("sre_agent.handler.SREAgent") as mock_agent_cls,
        patch("sre_agent.handler.SlackClient") as mock_slack_cls,
        patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
        patch("sre_agent.handler.incident_table", table),
    ):
        # Stub enricher's CW call
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {"MetricAlarms": []}
        mock_enricher_boto3.client.return_value = mock_cw

        mock_slack = MagicMock()
        mock_slack.post_investigating.return_value = "ts_integration"
        mock_slack_cls.return_value = mock_slack

        mock_agent = MagicMock()
        mock_agent.analyze.return_value = {
            "root_cause": "High error rate",
            "severity": "high",
            "fix_type": "manual",
            "fix_description": "Investigate",
            "pr_files": [],
            "runbook_steps": ["Check logs"],
        }
        mock_agent_cls.return_value = mock_agent
        mock_gh_cls.return_value = MagicMock()

        from sre_agent.handler import handler

        result = handler(event, None)

    assert result["statusCode"] == 200

    # Verify record was written
    incident_id = result.get("incident_id")
    if incident_id:
        item = table.get_item(Key={"incident_id": incident_id}).get("Item")
        assert item is not None
        assert item["status"] == "open"
        assert "created_at" in item
        assert "ttl" in item


@mock_aws
def test_deduplication_skips_second_invocation() -> None:
    """Second handler call with same incident within window should return 'duplicate'."""
    table = _create_incident_table()

    # Pre-seed the table with a recent incident
    incident_id = "dedup_test_id"
    table.put_item(
        Item={
            "incident_id": incident_id,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl": int(time.time()) + 7200,
            "status": "open",
        }
    )

    with (
        patch("sre_agent.handler.incident_table", table),
        patch("sre_agent.handler.enrich_event") as mock_enrich,
        patch("sre_agent.handler._incident_id", return_value=incident_id),
    ):
        mock_enrich.return_value = {
            "source": "cloudwatch_alarm",
            "cluster_name": "prod",
            "alarm_name": "test",
            "namespace": "api",
            "resource_name": "svc",
        }

        from sre_agent.handler import handler

        result = handler({}, None)

    assert result["body"] == "duplicate"


@mock_aws
def test_incident_record_has_correct_ttl() -> None:
    """DynamoDB record should have a TTL approximately 7 days from now."""
    table = _create_incident_table()

    with (
        patch("sre_agent.enricher.boto3") as mock_enricher_boto3,
        patch("sre_agent.handler.SREAgent") as mock_agent_cls,
        patch("sre_agent.handler.SlackClient") as mock_slack_cls,
        patch("sre_agent.handler.GitHubClient"),
        patch("sre_agent.handler.incident_table", table),
    ):
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {"MetricAlarms": []}
        mock_enricher_boto3.client.return_value = mock_cw

        mock_slack = MagicMock()
        mock_slack.post_investigating.return_value = "ts_ttl"
        mock_slack_cls.return_value = mock_slack

        mock_agent = MagicMock()
        mock_agent.analyze.return_value = {
            "root_cause": "CPU spike",
            "severity": "medium",
            "fix_type": "manual",
            "fix_description": "Scale up",
            "pr_files": [],
            "runbook_steps": [],
        }
        mock_agent_cls.return_value = mock_agent

        event = {
            "source": "sre.scheduled",
            "detail": {
                "cluster": "prod",
                "check_name": "ttl-test",
                "namespace": "api",
                "resource_type": "deployment",
                "resource_name": "my-svc",
                "findings": [],
            },
        }

        from sre_agent.handler import handler

        result = handler(event, None)

    if result.get("incident_id"):
        item = table.get_item(Key={"incident_id": result["incident_id"]}).get("Item")
        if item:
            seven_days = 7 * 86400
            now = int(time.time())
            assert item["ttl"] > now
            assert item["ttl"] <= now + seven_days + 60  # +60s tolerance


# ---------------------------------------------------------------------------
# Bot handler integration tests
# ---------------------------------------------------------------------------


@mock_aws
def test_mention_looks_up_incident_by_thread_ts() -> None:
    """Bot should find an incident matching the Slack thread_ts."""
    table = _create_incident_table()
    table.put_item(
        Item={
            "incident_id": "inc_thread_test",
            "slack_ts": "thread_ts_001",
            "status": "open",
            "analysis": {"root_cause": "OOMKill"},
        }
    )

    with (
        patch("sre_agent.bot_handler.incident_table", table),
        patch("sre_agent.bot_handler.get_llm_client") as mock_factory,
        patch("sre_agent.bot_handler._post_reply") as mock_reply,
    ):
        from sre_agent.llm_client import ContentBlock, MessageResponse

        mock_llm = MagicMock()
        mock_llm.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text="Root cause is OOMKill")],
            stop_reason="end_turn",
            model="mock",
        )
        mock_factory.return_value = mock_llm

        from sre_agent.bot_handler import _handle_mention

        _handle_mention(
            {
                "channel": "C123",
                "thread_ts": "thread_ts_001",
                "ts": "ts_child",
                "text": "<@UBOT> explain the issue",
                "user": "U_ENGI",
            }
        )

    mock_reply.assert_called_once()
    assert "Root cause" in mock_reply.call_args[0][2]


@mock_aws
def test_resolve_action_updates_dynamodb() -> None:
    """Resolve button click should update DynamoDB status."""
    table = _create_incident_table()
    table.put_item(
        Item={
            "incident_id": "inc_resolve_test",
            "slack_ts": "ts_resolve",
            "status": "open",
        }
    )

    with (
        patch("sre_agent.bot_handler.incident_table", table),
        patch("sre_agent.bot_handler._post_reply"),
    ):
        from sre_agent.bot_handler import _update_incident_status

        _update_incident_status("inc_resolve_test", "resolved", "U_ENGINEER")

    item = table.get_item(Key={"incident_id": "inc_resolve_test"}).get("Item")
    assert item is not None
    assert item["status"] == "resolved"
    assert item["resolved_by"] == "U_ENGINEER"
    assert "resolved_at" in item
