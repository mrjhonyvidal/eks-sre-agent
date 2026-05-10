from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

import boto3

from sre_agent.agent import SREAgent
from sre_agent.enricher import enrich_event
from sre_agent.github_client import GitHubClient
from sre_agent.shared.config import SharedConfig
from sre_agent.slack_client import SlackClient

logger = logging.getLogger(__name__)

AUTO_PR_SEVERITIES = {"critical", "high"}


class ProactiveIncidentFlow:
    def __init__(self, config: SharedConfig | None = None) -> None:
        self._config = config or SharedConfig.from_env()
        self._incident_table = boto3.resource("dynamodb").Table(self._config.incident_table)
        self._slack = SlackClient()
        self._github = GitHubClient()
        self._agent = SREAgent()

    def process(self, event: dict[str, Any]) -> dict[str, Any]:
        incident = enrich_event(event)
        incident_id = self._incident_id(incident)

        if self._is_duplicate(incident_id):
            return {"statusCode": 200, "body": "duplicate"}

        ts = self._slack.post_investigating(incident)

        try:
            analysis = self._agent.analyze(incident)
        except Exception as exc:
            logger.exception("Agent failure incident_id=%s", incident_id)
            self._slack.update_error(ts, str(exc))
            return {"statusCode": 500, "body": str(exc), "incident_id": incident_id}

        pr_url = self._maybe_open_pr(incident_id=incident_id, incident=incident, analysis=analysis)
        self._persist(incident_id=incident_id, incident=incident, analysis=analysis, ts=ts, pr_url=pr_url)
        self._slack.update_with_analysis(
            ts=ts,
            incident=incident,
            analysis=analysis,
            pr_url=pr_url,
            incident_id=incident_id,
        )

        return {"statusCode": 200, "body": "ok", "incident_id": incident_id}

    def _maybe_open_pr(
        self,
        *,
        incident_id: str,
        incident: dict[str, Any],
        analysis: dict[str, Any],
    ) -> str | None:
        if (
            analysis.get("fix_type") == "auto"
            and analysis.get("severity") in AUTO_PR_SEVERITIES
            and analysis.get("pr_files")
        ):
            try:
                return self._github.create_fix_pr(
                    incident_id=incident_id,
                    analysis=analysis,
                    incident=incident,
                )
            except Exception:
                logger.exception("PR creation failed incident_id=%s", incident_id)
        return None

    def _persist(
        self,
        *,
        incident_id: str,
        incident: dict[str, Any],
        analysis: dict[str, Any],
        ts: str,
        pr_url: str | None,
    ) -> None:
        record: dict[str, Any] = {
            "incident_id": incident_id,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl": int(time.time()) + 7 * 86400,
            "incident": incident,
            "analysis": analysis,
            "slack_ts": ts,
            "status": "open",
        }
        if pr_url:
            record["pr_url"] = pr_url
        self._incident_table.put_item(Item=record)

    @staticmethod
    def _incident_id(incident: dict[str, Any]) -> str:
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

    def _is_duplicate(self, incident_id: str, window_seconds: int = 1800) -> bool:
        cutoff = int(time.time()) - window_seconds
        try:
            resp = self._incident_table.get_item(Key={"incident_id": incident_id})
            item = resp.get("Item")
            if item:
                created = int(datetime.fromisoformat(item["created_at"]).timestamp())
                return created > cutoff
        except Exception:
            logger.exception("Dedup check failed incident_id=%s", incident_id)
        return False
