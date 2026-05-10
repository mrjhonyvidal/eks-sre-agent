"""
Lambda handler — EventBridge → SRE Agent.

Triggered by:
  - CloudWatch Alarm state changes (via EventBridge rule)
  - EKS control-plane / audit events matching error patterns
  - Scheduled health sweeps (cron)

Outputs:
  - Slack notification (always)
  - GitHub PR (when fix_type == "auto" and severity >= high)
  - DynamoDB incident record (always, for dedup + bot context)

Environment variables (required):
  ANTHROPIC_API_KEY or LLM_PROVIDER=bedrock
  SLACK_BOT_TOKEN
  SLACK_CHANNEL
  GITHUB_TOKEN
  GITHUB_REPO
  CLUSTER_NAME
  INCIDENT_TABLE

Environment variables (optional):
  DEPLOY_TABLE          (default: sre-deployments)
  GITHUB_BASE_BRANCH    (default: main)
  KUBECTL_LAMBDA        (default: sre-kubectl-helper)
  LLM_PROVIDER          (default: anthropic)
  BEDROCK_MODEL_ID      (default: us.anthropic.claude-sonnet-4-5-20250514-v1:0)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3

from sre_agent.agent import SREAgent
from sre_agent.enricher import enrich_event
from sre_agent.github_client import GitHubClient
from sre_agent.slack_client import SlackClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Severities that warrant an auto-PR attempt
AUTO_PR_SEVERITIES = {"critical", "high"}

# Module-level DynamoDB resource (reused across warm Lambda invocations)
_ddb = boto3.resource("dynamodb")
incident_table = _ddb.Table(os.environ.get("INCIDENT_TABLE", "sre-incidents"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main Lambda entry point."""
    logger.info("Received event source=%s", event.get("source", "unknown"))

    # 1. Normalise the raw EventBridge/CloudWatch payload
    incident = enrich_event(event)
    incident_id = _incident_id(incident)

    logger.info(
        "Processing incident_id=%s cluster=%s namespace=%s resource=%s",
        incident_id,
        incident.get("cluster_name"),
        incident.get("namespace"),
        incident.get("resource_name"),
    )

    # 2. Dedup — skip if we already processed this exact incident recently
    if _is_duplicate(incident_id):
        logger.info("Duplicate incident_id=%s — skipping", incident_id)
        return {"statusCode": 200, "body": "duplicate"}

    slack = SlackClient()
    gh = GitHubClient()
    agent = SREAgent()

    # 3. Post "investigating…" placeholder to Slack immediately
    ts = slack.post_investigating(incident)

    # 4. Run the agentic analysis (may take 10-30 s)
    try:
        analysis = agent.analyze(incident)
    except Exception as exc:
        logger.exception("Agent failed incident_id=%s: %s", incident_id, exc)
        slack.update_error(ts, str(exc))
        return {"statusCode": 500, "body": str(exc)}

    # 5. Build the persistence record
    pr_url = None
    record: dict[str, Any] = {
        "incident_id": incident_id,
        "created_at": datetime.now(UTC).isoformat(),
        "ttl": int(time.time()) + 7 * 86400,  # 7-day TTL
        "incident": incident,
        "analysis": analysis,
        "slack_ts": ts,
        "status": "open",
    }

    # 6. Raise a GitHub PR for auto-fixable, high-severity incidents
    if (
        analysis.get("fix_type") == "auto"
        and analysis.get("severity") in AUTO_PR_SEVERITIES
        and analysis.get("pr_files")
    ):
        try:
            pr_url = gh.create_fix_pr(
                incident_id=incident_id,
                analysis=analysis,
                incident=incident,
            )
            record["pr_url"] = pr_url
            logger.info("PR created incident_id=%s url=%s", incident_id, pr_url)
        except Exception as exc:
            logger.exception("PR creation failed incident_id=%s: %s", incident_id, exc)

    incident_table.put_item(Item=record)

    # 7. Update the Slack message with full analysis
    slack.update_with_analysis(
        ts=ts,
        incident=incident,
        analysis=analysis,
        pr_url=pr_url,
        incident_id=incident_id,
    )

    logger.info(
        "Incident handled incident_id=%s severity=%s fix_type=%s pr_url=%s",
        incident_id,
        analysis.get("severity"),
        analysis.get("fix_type"),
        pr_url,
    )

    return {"statusCode": 200, "body": "ok", "incident_id": incident_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _incident_id(incident: dict[str, Any]) -> str:
    """Stable SHA-256 hash of (source, cluster, namespace, resource, alarm) for dedup."""
    key = "|".join(
        [
            incident.get("source", ""),
            incident.get("cluster_name", ""),
            incident.get("namespace", ""),
            incident.get("resource_name", ""),
            incident.get("alarm_name", ""),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _is_duplicate(incident_id: str, window_seconds: int = 1800) -> bool:
    """Return True if this incident_id was seen within the last `window_seconds`."""
    cutoff = int(time.time()) - window_seconds
    try:
        resp = incident_table.get_item(Key={"incident_id": incident_id})
        item = resp.get("Item")
        if item:
            created = int(datetime.fromisoformat(item["created_at"]).timestamp())
            return created > cutoff
    except Exception as exc:
        logger.warning("Dedup check failed for incident_id=%s: %s", incident_id, exc)
    return False
