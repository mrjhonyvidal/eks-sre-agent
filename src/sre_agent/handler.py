"""
Backward-compatible proactive Lambda entrypoint.

The implementation now lives in `sre_agent.proactive.handler`.
"""

from datetime import datetime

from sre_agent.agent import SREAgent
from sre_agent.enricher import enrich_event
from sre_agent.github_client import GitHubClient
from sre_agent.proactive.flow import ProactiveIncidentFlow
from sre_agent.proactive.handler import handler as handler
from sre_agent.slack_client import SlackClient

_flow = None
incident_table = None


def _get_flow() -> ProactiveIncidentFlow:
    global _flow, incident_table
    if _flow is None:
        _flow = ProactiveIncidentFlow()
        incident_table = _flow._incident_table
    return _flow


def _incident_id(incident: dict) -> str:
    return ProactiveIncidentFlow._incident_id(incident)


def _is_duplicate(incident_id: str, window_seconds: int = 1800) -> bool:
    return _get_flow()._is_duplicate(incident_id=incident_id, window_seconds=window_seconds)


__all__ = [
    "GitHubClient",
    "SREAgent",
    "SlackClient",
    "_incident_id",
    "_is_duplicate",
    "datetime",
    "enrich_event",
    "handler",
    "incident_table",
]
