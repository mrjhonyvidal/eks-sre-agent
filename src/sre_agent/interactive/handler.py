from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3

from sre_agent.interactive.orchestrator import K8sOrchestratorAgent
from sre_agent.shared.config import SharedConfig
from sre_agent.slack_client import SlackClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_cfg = SharedConfig.from_env()
_incident_table = None
_orchestrator = None
incident_table = None


def _get_incident_table() -> Any:
    global _incident_table, incident_table
    if _incident_table is None:
        _incident_table = boto3.resource("dynamodb").Table(_cfg.incident_table)
        incident_table = _incident_table
    return _incident_table


def _get_orchestrator() -> K8sOrchestratorAgent:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = K8sOrchestratorAgent()
    return _orchestrator


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    body_raw = event.get("body", "{}")
    body = json.loads(body_raw)

    if body.get("type") == "url_verification":
        return {"statusCode": 200, "body": json.dumps({"challenge": body["challenge"]})}

    if not _verify_slack_signature(event):
        return {"statusCode": 401, "body": "Unauthorized"}

    if body.get("type") == "event_callback":
        slack_event = body.get("event", {})
        if slack_event.get("type") == "app_mention":
            _handle_mention(slack_event)
    elif body.get("type") == "block_actions":
        _handle_block_action(body)
    return {"statusCode": 200, "body": "ok"}


def _handle_mention(event: dict[str, Any]) -> None:
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    text: str = event.get("text", "")
    user = event.get("user", "unknown")
    question = text.split(">", 1)[-1].strip() if ">" in text else text.strip()

    incident = _find_incident_from_thread(thread_ts) or {}
    if not incident:
        _post_reply(
            channel,
            thread_ts,
            "I couldn't find an active incident in this thread. "
            "Use me in an incident thread or provide an incident ID.",
        )
        return

    try:
        answer = _get_orchestrator().respond(question=question, incident_context=incident)
        _post_reply(channel, thread_ts, f"<@{user}> {answer}")
    except Exception as exc:
        logger.exception("Interactive query failed")
        _post_reply(channel, thread_ts, f":x: Query failed: {exc}")


def _handle_block_action(body: dict[str, Any]) -> None:
    actions = body.get("actions", [])
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")
    channel = body.get("channel", {}).get("id", "")
    message_ts = body.get("message", {}).get("ts", "")
    user = body.get("user", {}).get("id", "unknown")

    parts = value.split("|", 1)
    incident_id = parts[1] if len(parts) == 2 else ""

    if action_id == "ask_agent":
        _post_reply(
            channel,
            message_ts,
            f"<@{user}> Ask any EKS question here (MCP-backed), e.g. "
            "`@SREBot show unhealthy pods in kube-system`",
        )
    elif action_id == "resolve_incident":
        _update_incident_status(incident_id, "resolved", user)
        _post_reply(channel, message_ts, f"<@{user}> marked incident `{incident_id}` as resolved.")
    elif action_id == "false_positive":
        _update_incident_status(incident_id, "false_positive", user)
        _post_reply(
            channel, message_ts, f"<@{user}> marked incident `{incident_id}` as false positive."
        )


def _find_incident_from_thread(thread_ts: str) -> dict[str, Any] | None:
    try:
        resp = _get_incident_table().scan(
            FilterExpression="slack_ts = :ts",
            ExpressionAttributeValues={":ts": thread_ts},
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception:
        logger.exception("Incident lookup failed")
        return None


def _update_incident_status(incident_id: str, status: str, user: str) -> None:
    _get_incident_table().update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET #s = :s, resolved_by = :u, resolved_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": user, ":t": datetime.now(UTC).isoformat()},
    )


def _post_reply(channel: str, thread_ts: str, text: str) -> None:
    slack = SlackClient()
    slack.post_thread_reply(thread_ts=thread_ts, text=text)


def _verify_slack_signature(event: dict[str, Any]) -> bool:
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        return True
    headers = event.get("headers", {})
    ts = headers.get("x-slack-request-timestamp", "0")
    sig_header = headers.get("x-slack-signature", "")
    body = event.get("body", "")
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            f"v0:{ts}:{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, sig_header)
