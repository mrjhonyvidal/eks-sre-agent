"""
Slack client — posts and updates incident messages using Block Kit.

Wraps the official slack-sdk `WebClient` so we get retry/rate-limit handling,
`SlackApiError` exceptions, and proper type hints out of the box.

Required env vars:
  SLACK_BOT_TOKEN   — xoxb-… bot OAuth token
  SLACK_CHANNEL     — #sre-alerts (or channel ID)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "critical": "#E53E3E",
    "high": "#DD6B20",
    "medium": "#D69E2E",
    "low": "#38A169",
}

SEVERITY_EMOJI = {
    "critical": ":red_circle:",
    "high": ":large_orange_circle:",
    "medium": ":large_yellow_circle:",
    "low": ":large_green_circle:",
}


class SlackClient:
    def __init__(self) -> None:
        self.token = os.environ["SLACK_BOT_TOKEN"]
        self.channel = os.environ["SLACK_CHANNEL"]
        self._web = WebClient(token=self.token)

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def post_investigating(self, incident: dict) -> str:
        """Post the initial 'investigating…' message. Returns the message timestamp (ts)."""
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":mag: SRE Agent — Investigating incident…"},
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Service:*\n`{incident.get('resource_name', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Namespace:*\n`{incident.get('namespace', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Cluster:*\n`{incident.get('cluster_name', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Alarm:*\n{incident.get('alarm_name', 'unknown')}",
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_Collecting logs, metrics, and k8s state…_"}
                ],
            },
        ]
        resp = self._post("chat.postMessage", {"channel": self.channel, "blocks": blocks})
        return resp.get("ts", "")

    def update_with_analysis(
        self,
        ts: str,
        incident: dict,
        analysis: dict,
        pr_url: str | None,
        incident_id: str,
    ) -> None:
        """Replace the investigating message with the full analysis."""
        severity = analysis.get("severity", "medium")
        emoji = SEVERITY_EMOJI.get(severity, ":white_circle:")

        runbook_text = (
            "\n".join(f"• {s}" for s in analysis.get("runbook_steps", [])[:6])
            or "_No steps generated._"
        )

        pr_section: list[dict] = []
        if pr_url:
            pr_section = [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":twisted_rightwards_arrows: *Auto-fix PR opened:*\n<{pr_url}|View PR>",
                    },
                },
            ]

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Incident: {incident.get('resource_name', 'unknown')}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:*\n`{severity.upper()}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Fix type:*\n`{analysis.get('fix_type', 'manual')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Namespace:*\n`{incident.get('namespace', 'unknown')}`",
                    },
                    {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":mag: *Root cause*\n{analysis.get('root_cause', 'N/A')}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":wrench: *Fix*\n{analysis.get('fix_description', 'N/A')}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":clipboard: *Runbook*\n{runbook_text}",
                },
            },
            *pr_section,
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":speech_balloon: Ask agent"},
                        "value": f"ask|{incident_id}",
                        "action_id": "ask_agent",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":white_check_mark: Resolve"},
                        "value": f"resolve|{incident_id}",
                        "action_id": "resolve_incident",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":no_entry: False positive"},
                        "value": f"fp|{incident_id}",
                        "action_id": "false_positive",
                        "style": "danger",
                    },
                ],
            },
        ]

        self._post(
            "chat.update",
            {"channel": self.channel, "ts": ts, "blocks": blocks},
        )

    def update_error(self, ts: str, error: str) -> None:
        """Update the message to show that analysis failed."""
        self._post(
            "chat.update",
            {
                "channel": self.channel,
                "ts": ts,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":x: *SRE Agent analysis failed*\n```{error[:500]}```",
                        },
                    }
                ],
            },
        )

    def post_thread_reply(self, thread_ts: str, text: str) -> None:
        """Post a reply inside a thread (used by the bot for follow-up chat)."""
        self._post(
            "chat.postMessage",
            {"channel": self.channel, "thread_ts": thread_ts, "text": text},
        )

    # ------------------------------------------------------------------ #
    #  HTTP helper                                                         #
    # ------------------------------------------------------------------ #

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a Slack Web API call via slack-sdk.

        Kept as a single method so tests can patch `SlackClient._post`
        regardless of the underlying transport.
        """
        try:
            response = self._web.api_call(method, json=payload)
        except SlackApiError as exc:
            logger.error("Slack API error (%s): %s", method, exc.response.data)
            return dict(exc.response.data)
        return dict(response.data)
