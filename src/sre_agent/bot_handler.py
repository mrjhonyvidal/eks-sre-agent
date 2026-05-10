"""
Slack Bot Lambda handler — interactive chat with the SRE agent.

Handles:
  - @mention messages in Slack (ask questions about active incidents)
  - Button actions (resolve, false-positive, ask-agent)

Architecture note:
  - This is a SEPARATE Lambda from handler.py to keep concerns isolated.
  - The bot must respond within 3 s (Slack timeout); the agent Lambda can run
    up to 5 minutes. Keeping them separate avoids cold-start conflicts.
  - Uses the same SREAgent and DynamoDB table for context.
  - Deployed behind API Gateway with the Slack Events API URL.

Required env vars (same as handler.py, plus):
  SLACK_SIGNING_SECRET   — for request verification
"""

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

from sre_agent.llm_client import get_llm_client
from sre_agent.slack_client import SlackClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ddb = boto3.resource("dynamodb")
incident_table = _ddb.Table(os.environ.get("INCIDENT_TABLE", "sre-incidents"))

SYSTEM_PROMPT_BOT = """You are an interactive SRE assistant embedded in Slack.
You have access to the incident record below. Answer concisely — this is a chat interface.
Focus on actionable, specific guidance. Use plain text, no markdown headers.
If asked to run commands, provide the exact kubectl / aws-cli commands.
Keep responses under 400 words unless explicitly asked for more detail.

Incident context:
{incident_json}
"""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway Lambda proxy handler."""
    body_raw = event.get("body", "{}")

    # Slack URL verification challenge (one-time setup)
    body = json.loads(body_raw)
    if body.get("type") == "url_verification":
        return {"statusCode": 200, "body": json.dumps({"challenge": body["challenge"]})}

    # Verify the request is genuinely from Slack
    if not _verify_slack_signature(event):
        logger.warning("Invalid Slack signature — rejecting request")
        return {"statusCode": 401, "body": "Unauthorized"}

    event_type = body.get("type")

    if event_type == "event_callback":
        slack_event = body.get("event", {})
        if slack_event.get("type") == "app_mention":
            _handle_mention(slack_event)
    elif event_type == "block_actions":
        _handle_block_action(body)
    else:
        logger.debug("Unhandled Slack event type: %s", event_type)

    return {"statusCode": 200, "body": "ok"}


# ---------------------------------------------------------------------------
# Mention handler (@SREBot <question>)
# ---------------------------------------------------------------------------


def _handle_mention(event: dict[str, Any]) -> None:
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    text: str = event.get("text", "")
    user = event.get("user", "unknown")

    # Strip the bot mention (e.g. "<@U123ABC> show me the logs")
    question = text.split(">", 1)[-1].strip() if ">" in text else text.strip()
    logger.info("Mention from user=%s thread_ts=%s question_len=%d", user, thread_ts, len(question))

    # Look up the active incident for this thread
    incident = _find_incident_from_thread(thread_ts) or {}

    if not incident:
        _post_reply(
            channel,
            thread_ts,
            "I couldn't find an active incident in this thread. "
            "Tag me in a thread that has an SRE Agent alert, or mention an incident ID.",
        )
        return

    system = SYSTEM_PROMPT_BOT.format(incident_json=json.dumps(incident, indent=2, default=str))

    try:
        llm = get_llm_client()
        resp = llm.create_message(
            system=system,
            messages=[{"role": "user", "content": question}],
            tools=[],
            max_tokens=1024,
        )
        text_blocks = [b for b in resp.content if b.type == "text"]
        answer = text_blocks[0].text if text_blocks else "Sorry, I couldn't generate a response."
        _post_reply(channel, thread_ts, f"<@{user}> {answer}")
    except Exception as exc:
        logger.error("Bot error for user=%s: %s", user, exc, exc_info=True)
        _post_reply(channel, thread_ts, f":x: Agent error: {exc}")


# ---------------------------------------------------------------------------
# Block action handler (button clicks)
# ---------------------------------------------------------------------------


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

    logger.info("Block action action_id=%s incident_id=%s user=%s", action_id, incident_id, user)

    if action_id == "ask_agent":
        _post_reply(
            channel,
            message_ts,
            f"<@{user}> :wave: Sure! Tag me in this thread with your question, e.g.:\n"
            f"`@SREBot What caused the OOMKill?`\nIncident ID: `{incident_id}`",
        )
    elif action_id == "resolve_incident":
        _update_incident_status(incident_id, "resolved", user)
        _post_reply(
            channel,
            message_ts,
            f"<@{user}> :white_check_mark: Marked incident `{incident_id}` as resolved.",
        )
    elif action_id == "false_positive":
        _update_incident_status(incident_id, "false_positive", user)
        _post_reply(
            channel,
            message_ts,
            f"<@{user}> :no_entry: Marked incident `{incident_id}` as false positive. "
            "Thanks for the feedback!",
        )
    else:
        logger.warning("Unknown action_id=%s", action_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_incident_from_thread(thread_ts: str) -> dict[str, Any] | None:
    """Scan incidents for the one whose slack_ts matches this thread."""
    try:
        resp = incident_table.scan(
            FilterExpression="slack_ts = :ts",
            ExpressionAttributeValues={":ts": thread_ts},
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception as exc:
        logger.warning("Could not look up incident by thread_ts=%s: %s", thread_ts, exc)
        return None


def _update_incident_status(incident_id: str, status: str, user: str) -> None:
    try:
        incident_table.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :s, resolved_by = :u, resolved_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":u": user,
                ":t": datetime.now(UTC).isoformat(),
            },
        )
        logger.info("Updated incident_id=%s status=%s by user=%s", incident_id, status, user)
    except Exception as exc:
        logger.error("Could not update incident_id=%s status: %s", incident_id, exc, exc_info=True)


def _post_reply(channel: str, thread_ts: str, text: str) -> None:
    slack = SlackClient()
    slack.post_thread_reply(thread_ts=thread_ts, text=text)


def _verify_slack_signature(event: dict[str, Any]) -> bool:
    """HMAC-SHA256 request verification per Slack docs."""
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping Slack signature verification")
        return True

    headers = event.get("headers", {})
    ts = headers.get("x-slack-request-timestamp", "0")
    sig_header = headers.get("x-slack-signature", "")
    body = event.get("body", "")

    # Reject requests older than 5 minutes (replay attack prevention)
    try:
        if abs(time.time() - int(ts)) > 300:
            logger.warning("Slack timestamp too old: %s", ts)
            return False
    except ValueError:
        return False

    base_string = f"v0:{ts}:{body}"
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            base_string.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(expected, sig_header)
